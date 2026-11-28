"""Offline gateway routing, completion accounting and worker diagnostics."""
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from advnet.contracts import Observation
from advnet.persistent_controller import EngineProvider, _infer
from advnet.pipeline import DecisionEngine
from advnet.providers import LLM, MockLLM, ProviderResponseError, safe_reply_metadata
from advnet.visualizer_data import _decision


ROOT = Path(__file__).resolve().parents[1]


class ProviderTests(unittest.TestCase):
    def response(self, reason='stop', content='{}'):
        return SimpleNamespace(model='example/model', id='gen-123', provider='Wafer',
            choices=[SimpleNamespace(finish_reason=reason, message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(model_dump=lambda **kw: {
                'prompt_tokens': 100, 'completion_tokens': 4096, 'cost': .06}))

    def sdk(self, response):
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.chat.completions.create.return_value = response
        return client, SimpleNamespace(OpenAI=Mock(return_value=client))

    def test_pinning_is_sent_and_default_routing_is_unchanged(self):
        for provider in (None, 'wafer'):
            with self.subTest(provider=provider):
                client, sdk = self.sdk(self.response())
                with patch.dict(sys.modules, {'openai': sdk}), patch.dict(os.environ, OPEN_ROUTER_KEY='test'):
                    reply = LLM('openrouter', 'example/model', 30, 4096,
                                endpoint_provider=provider).propose('system', {})
                request = client.chat.completions.create.call_args.kwargs
                self.assertEqual(request['max_tokens'], 4096)
                if provider:
                    self.assertEqual(request['extra_body'], {'provider': {
                        'only': ['wafer'], 'allow_fallbacks': False, 'require_parameters': True}})
                    self.assertEqual(reply.metadata['requested_provider'], provider)
                else:
                    self.assertNotIn('extra_body', request)
                self.assertEqual(reply.metadata['provider'], 'Wafer')
                self.assertEqual(reply.metadata['generation_id'], 'gen-123')

    def test_truncation_retains_billing_through_pipeline_and_worker(self):
        client, sdk = self.sdk(self.response('length', 'private incomplete response'))
        observation = Observation.model_validate_json((ROOT/'examples/congestion.json').read_text())
        llm = LLM('openrouter', 'example/model', 30, 4096, endpoint_provider='wafer')
        with TemporaryDirectory() as folder, patch.dict(sys.modules, {'openai': sdk}), \
                patch.dict(os.environ, OPEN_ROUTER_KEY='test'):
            destination = Path(folder)/'decision.json'
            _infer(EngineProvider(llm), ('llm_only', observation, [], 1), destination)
            result = json.loads(destination.read_text())
        self.assertEqual(result['status'], 'decision_error')
        self.assertIsNone(result['proposal'])
        self.assertEqual(result['provider_error_code'], 'output_token_limit')
        self.assertEqual(result['error_stage'], 'provider_response')
        self.assertEqual(result['metrics']['llm_output_tokens'], 4096)
        self.assertEqual(result['metrics']['llm_cost_usd'], .06)
        self.assertEqual(result['calls'][0]['metadata']['finish_reason'], 'length')
        self.assertEqual(result['calls'][0]['error_type'], 'ProviderResponseError')
        self.assertNotIn('private incomplete response', json.dumps(result))
        view = _decision(result)
        self.assertEqual(view['provider_error_code'], 'output_token_limit')
        self.assertEqual(view['calls'][0]['metadata']['finish_reason'], 'length')

    def test_request_error_retains_http_diagnostics_and_unknown_billing(self):
        class RateLimitError(Exception):
            status_code = 429
            code = 'rate_limit_exceeded'
        client, sdk = self.sdk(self.response())
        client.chat.completions.create.side_effect = RateLimitError('private provider body')
        observation = Observation.model_validate_json((ROOT/'examples/congestion.json').read_text())
        with TemporaryDirectory() as folder, patch.dict(sys.modules, {'openai': sdk}), \
                patch.dict(os.environ, OPEN_ROUTER_KEY='test'):
            destination = Path(folder)/'decision.json'
            _infer(EngineProvider(LLM('openrouter', 'example/model', 30, 4096)),
                   ('llm_only', observation, [], 1), destination)
            result = json.loads(destination.read_text())
        self.assertEqual(result['http_status'], 429)
        self.assertEqual(result['error_stage'], 'provider_request')
        self.assertEqual(result['provider_error_code'], 'rate_limit_exceeded')
        self.assertEqual(result['metrics']['llm_calls'], 1)
        self.assertIsNone(result['metrics']['llm_cost_usd'])
        self.assertIsNone(result['proposal'])
        self.assertNotIn('private provider body', json.dumps(result))

    def test_empty_completions_are_accounted_without_returning_a_proposal(self):
        for reason, content, code in [('stop', '', 'empty_content'),
                                      ('content_filter', None, 'unsupported_finish_reason'),
                                      (None, None, 'empty_response')]:
            response = self.response(reason, content)
            if reason is None:
                response.choices = []
            client, sdk = self.sdk(response)
            with self.subTest(code=code), patch.dict(sys.modules, {'openai': sdk}), \
                    patch.dict(os.environ, OPEN_ROUTER_KEY='test'):
                with self.assertRaises(ProviderResponseError) as raised:
                    LLM('openrouter', 'example/model', 30, 4096).propose('system', {})
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.reply.usage['cost'], .06)

    def test_parse_and_scope_failures_have_distinct_safe_diagnostics(self):
        observation = Observation.model_validate_json((ROOT/'examples/congestion.json').read_text())
        bad_scope = json.loads((ROOT/'examples/mock-proposal.json').read_text())
        bad_scope['changes'][0]['device'] = 'outside-lab'
        for proposal, stage in [({'secret': 'private payload'}, 'proposal_parsing'),
                                (bad_scope, 'scope_validation')]:
            result = DecisionEngine(None, MockLLM(proposal), {}).decide(observation)
            self.assertEqual(result['status'], 'decision_error')
            self.assertEqual(result['error_stage'], stage)
            self.assertNotIn('private payload', json.dumps(result))
            self.assertEqual(len(result['calls']), 1)

    def test_metadata_excludes_unapproved_fields_and_content(self):
        self.assertEqual(safe_reply_metadata({'provider': 'Wafer', 'finish_reason': 'secret text',
                                             'content': 'private', 'generation_id': '<private>'}),
                         {'provider': 'Wafer'})


if __name__ == '__main__':
    unittest.main()
