"""Measured inference accounting; absent usage and prices remain unknown."""


def numeric(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool)


def token_value(call, output=False):
    usage=call.get('usage') or {}
    return usage.get('output_tokens' if output else 'input_tokens',
                     usage.get('completion_tokens' if output else 'prompt_tokens'))


def reported_cost(call):
    usage=call.get('usage') or {}
    value=usage.get('cost',usage.get('cost_usd'))
    return value if numeric(value) and value>=0 else None


def known_sum(values):
    return sum(values) if all(numeric(value) for value in values) else None


def inference_metrics(calls):
    result={}
    for stage in ('jev','llm'):
        selected=[c for c in calls if c.get('stage','llm')==stage]
        result[stage+'_calls']=len(selected)
        result[stage+'_input_tokens']=known_sum([token_value(c) for c in selected])
        result[stage+'_output_tokens']=known_sum([token_value(c,True) for c in selected])
        elapsed=known_sum([c.get('elapsed_ms') for c in selected])
        result[stage+'_seconds']=elapsed/1000 if elapsed is not None else None
        result[stage+'_cost_usd']=known_sum([reported_cost(c) for c in selected])
    result['input_tokens']=known_sum([token_value(c) for c in calls])
    result['output_tokens']=known_sum([token_value(c,True) for c in calls])
    result['cost_usd']=known_sum([reported_cost(c) for c in calls])
    return result
