#!/usr/bin/env python3
"""Plot reviewed repeated latency-campaign analysis; export the plotted samples.

Use an interpreter with matplotlib. Input is a sanitized analysis JSON with
cohort and stages fields, not a live lab directory. No network access is used.
"""
import argparse
import csv
from datetime import datetime
import json
from pathlib import Path


def plot(analysis, prefix):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    plt.rcParams.update({'font.size': 9, 'svg.fonttype': 'none'})
    cohort = analysis['cohort']
    fig, axes = plt.subplots(len(cohort), 2, figsize=(11.5, 2.7*len(cohort)), sharex=True, squeeze=False)
    exported = []
    for index, trial in enumerate(cohort):
        repetition = trial['repetition']
        pair = axes[index]
        for axis in pair:
            axis.axvspan(0, 300, color='#f4deb7', alpha=.45, label='Injected delay active' if index == 0 else None)
            axis.axvline(0, color='#777777', linewidth=.7)
            axis.axvline(300, color='#777777', linewidth=.7)
            axis.grid(alpha=.2)
            axis.set_xlim(-65, 425)
        for arm, color, label, style in [('observe', '#747474', 'Observe-only control', '--'),
                                          ('jev', '#1764a0', 'Jev + Terra', '-')]:
            stage = analysis['stages'][repetition+'-'+arm]
            windows = stage['periodic_windows']
            for window in windows:
                exported.append({'repetition': repetition, 'arm': arm, 'kind': 'periodic',
                                 'seconds_from_fault': window['seconds_from_fault'],
                                 'mean_probe_rtt_ms': window['mean_probe_rtt_ms'],
                                 'max_link_utilization': window['max_link_utilization'],
                                 'error': window['error']})
            valid = [w for w in windows if not w['error']]
            if arm == 'jev':
                fault = datetime.fromisoformat(stage['summary']['fault_applied_at'])
                for event in stage['runtime_acceptance_events']:
                    window = event['windows'][2]
                    start = datetime.fromisoformat(window['start'].replace('Z', '+00:00'))
                    end = datetime.fromisoformat(window['end'].replace('Z', '+00:00'))
                    valid.append({**window, 'seconds_from_fault':
                                  ((start-fault).total_seconds()+(end-fault).total_seconds())/2})
                valid.sort(key=lambda w: w['seconds_from_fault'])
            for axis, field, scale in [(pair[0], 'mean_probe_rtt_ms', 1), (pair[1], 'max_link_utilization', 100)]:
                samples = [w for w in valid if w[field] is not None]
                axis.plot([w['seconds_from_fault'] for w in samples], [w[field]*scale for w in samples],
                          color=color, linestyle=style, linewidth=1.3, marker='.', markersize=3, label=label)
            if arm == 'jev':
                fault = datetime.fromisoformat(stage['summary']['fault_applied_at'])
                for event in stage['runtime_acceptance_events']:
                    window = event['windows'][2]
                    start = datetime.fromisoformat(window['start'].replace('Z', '+00:00'))
                    end = datetime.fromisoformat(window['end'].replace('Z', '+00:00'))
                    moment = ((start-fault).total_seconds()+(end-fault).total_seconds())/2
                    accepted = event['accepted']
                    kind = 'candidate accepted' if accepted else 'candidate rejected'
                    exported.append({'repetition': repetition, 'arm': arm, 'kind': kind,
                                     'seconds_from_fault': moment, 'mean_probe_rtt_ms': window['mean_probe_rtt_ms'],
                                     'max_link_utilization': window['max_link_utilization'], 'error': window['error']})
                    for axis, field, scale in [(pair[0], 'mean_probe_rtt_ms', 1), (pair[1], 'max_link_utilization', 100)]:
                        axis.scatter([moment], [window[field]*scale], marker='o' if accepted else 'X',
                                     color='#137c49' if accepted else '#c45518', s=45, zorder=5,
                                     edgecolors='white', linewidths=.5, label=kind.capitalize())
        limit = trial['first_repair_windows'][0]['mean_probe_rtt_ms']*1.25+5
        pair[0].axhline(limit, color='#9f4141', linestyle=':', linewidth=1, label='Calibrated RTT limit')
        pair[0].set_ylim(0, 120)
        pair[1].set_ylim(0, 100)
        pair[0].set_ylabel('Mean probe RTT (ms)')
        pair[1].set_ylabel('Peak directional utilization (%)')
        accepted_at = trial['fault_to_accepted_repair_seconds']
        pair[0].set_title(f'{repetition.upper()}: repair accepted at {accepted_at:.1f} s', loc='left')
        pair[1].set_title(f'{repetition.upper()}: repair and later optimization', loc='left')
    for axis in axes[-1]:
        axis.set_xlabel('Seconds since fault injection')
    handles, labels = [], []
    for row in axes:
        for axis in row:
            hs, ls = axis.get_legend_handles_labels()
            for handle, label in zip(hs, ls):
                if label not in labels:
                    handles.append(handle); labels.append(label)
    fig.legend(handles, labels, loc='lower center', ncol=3, frameon=False, bbox_to_anchor=(.5, .025))
    fig.suptitle('Repeated Terra latency recovery: three fresh matched pairs', fontsize=13)
    fig.text(.5, .009, 'Lines join measurement windows. Markers show candidate-validation windows; rejected changes can cause temporary regressions.',
             ha='center', fontsize=8)
    fig.tight_layout(rect=(0, .12, 1, .96))
    prefix.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(prefix)+'.png', dpi=180)
    fig.savefig(str(prefix)+'.svg')
    plt.close(fig)
    with Path(str(prefix)+'.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(exported[0]))
        writer.writeheader(); writer.writerows(exported)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('analysis', type=Path)
    cli.add_argument('--output-prefix', type=Path, required=True)
    args = cli.parse_args()
    plot(json.loads(args.analysis.read_text()), args.output_prefix)
    print('Written PNG, SVG and sample CSV:', args.output_prefix)


if __name__ == '__main__':
    main()
