"""Read preserved predictions; implement published formulas with explicit local conventions.

No inference, no original result writes. Label/injection reads are external scoring only.
"""
from pathlib import Path
from functools import lru_cache
from collections import Counter, defaultdict, deque
import csv
import hashlib
import json
import re

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[1]
DATA = ROOT.parent / 'OpenRCA_v2_dataset' / 'cases'
HASHES = {}

def read(path):
    path = Path(path)
    raw = path.read_bytes()
    HASHES[str(path)] = hashlib.sha256(raw).hexdigest()
    return json.loads(raw.decode('utf-8-sig'))

def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

def norm_service(value):
    value = str(value).strip().lower()
    if value.startswith('ts-'):
        value = value[3:]
    return value.replace('-', '').replace('_', '')

def compact(value):
    return re.sub('[^a-z0-9]', '', str(value or '').lower())

GROUPS = {
 'pod_failure': ['PodKill', 'ContainerKill'],
 'pod_unavailable': ['PodFailure'],
 'network_delay': ['NetworkDelay'], 'network_loss': ['NetworkLoss'],
 'network_partition': ['NetworkPartition'], 'network_corrupt': ['NetworkCorrupt'],
 'network_duplicate': ['NetworkDuplicate'], 'network_bandwidth_limit': ['NetworkBandwidth'],
 'http_aborted': ['HTTPRequestAbort', 'HTTPResponseAbort'],
 'http_slow': ['HTTPRequestDelay', 'HTTPResponseDelay'],
 'http_payload_modified': ['HTTPResponseReplaceBody', 'HTTPResponsePatchBody',
                           'HTTPRequestReplacePath', 'HTTPRequestReplaceMethod'],
 'http_response_status_modified': ['HTTPResponseReplaceCode'],
 'cpu_stress': ['CPUStress'], 'mem_stress': ['MemoryStress'],
 'jvm_thread_cpu_stress': ['JVMCPUStress'], 'jvm_heap_stress': ['JVMMemoryStress'],
 'jvm_gc_pressure': ['JVMGarbageCollector'], 'jvm_method_exception': ['JVMException'],
 'jvm_jdbc_exception': ['JVMMySQLException'], 'jvm_method_latency': ['JVMLatency'],
 'jvm_jdbc_latency': ['JVMMySQLLatency'], 'jvm_method_mutated': ['JVMReturn', 'JVMRuntimeMutator'],
 'dns_resolution_failed': ['DNSError', 'DNSChaos'], 'dns_resolution_wrong': ['DNSRandom'],
 'clock_skew': ['TimeSkew', 'TimeChaos'],
}
KINDS = {compact(s): target for target, sources in GROUPS.items() for s in [target, *sources]}
LOAD = {'loadgenerator', 'loadgen', 'servicetest'}

def norm_kind(value, alias=False):
    key = compact(value)
    if key in ('', 'null', 'none', 'unknown'):
        return '__unknown__'
    if alias:
        key = {'stresscpu': 'cpustress', 'stressmemory': 'memorystress'}.get(key, key)
    return KINDS.get(key, '__unmapped__:' + key)

def prf(pred, gold):
    pred, gold = set(pred), set(gold)
    tp = len(pred & gold)
    p = tp / len(pred) if pred else (1.0 if not gold else 0.0)
    r = tp / len(gold) if gold else 1.0
    return dict(precision=p, recall=r, f1=2*p*r/(p+r) if p+r else 0.0,
                tp=tp, fp=len(pred-gold), fn=len(gold-pred), exact=pred == gold)

def decoded(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value or {}

def network_endpoints(label, injection, fault):
    """Explicit injection configuration only, never guessing from container names."""
    if not str(fault['chaos_type']).startswith('Network'):
        return [], None
    index = fault['index']
    engine = decoded(injection.get('engine_config'))
    if isinstance(engine, list) and index < len(engine):
        record = engine[index]
        if (isinstance(record, dict) and compact(record.get('chaos_type')) == compact(fault['chaos_type'])
            and isinstance(record.get('app'), str) and isinstance(record.get('target_service'), str)):
            return [record['app'], record['target_service']], 'engine_config[index].app,target_service'
    display = decoded(injection.get('display_config'))
    point = display.get('injection_point', {}) if isinstance(display, dict) else {}
    if (len(label['faults']) == 1 and index == 0 and isinstance(point.get('source_service'), str)
        and isinstance(point.get('target_service'), str)):
        return [point['source_service'], point['target_service']], 'display_config.injection_point'
    return [], 'explicit_network_endpoints_unavailable'

@lru_cache(None)
def truth(case, network=True, include_graph_roots=False):
    lab = read(DATA/case/'label.json')
    graph = read(DATA/case/'causal_graph.json')
    injection = read(DATA/case/'injection.json')
    faults, endpoint_audit = [], []
    for f in lab['faults']:
        explicit = f.get('also_accept') or []
        if isinstance(explicit, str): explicit = [explicit]
        ends, source = network_endpoints(lab, injection, f)
        accepted = [f['service'], *explicit, *(ends if network else [])]
        faults.append({'service': norm_service(f['service']), 'services': sorted(set(map(norm_service, accepted))),
                       'kind': norm_kind(f['chaos_type']), 'role': f['role'], 'index': f['index']})
        if source:
            endpoint_audit.append({'index': f['index'], 'role': f['role'], 'type': f['chaos_type'],
                                   'endpoints': ends, 'source': source})
    c2s = graph.get('component_to_service', {})
    def project(node):
        name = node.get('component') if isinstance(node, dict) else node
        service = c2s.get(name)
        return norm_service(service) if service else None
    unmapped = [n.get('component') for key in ('nodes', 'root_causes', 'alarm_nodes')
                for n in graph.get(key, []) if project(n) is None]
    assert not unmapped, (case, unmapped)
    edges = {tuple(map(norm_service, e)) for e in lab.get('service_edges', [])}
    edges = {e for e in edges if not (set(e) & LOAD) and e[0] != e[1]}
    raw_nodes = {project(n) for n in graph.get('nodes', [])} - LOAD - {None}
    extra = {s for e in edges for s in e}
    if include_graph_roots:
        extra |= {project(n) for n in graph.get('root_causes', [])}
    nodes = (raw_nodes | extra) - LOAD - {None}
    alarms = {project(n) for n in graph.get('alarm_nodes', [])} - LOAD - {None}
    return dict(faults=faults, edges=edges, nodes=nodes, alarms=alarms, system=lab['system'],
                process_applicable=lab['process_applicable'], endpoint_audit=endpoint_audit,
                added_nodes=sorted(nodes-raw_nodes))

def root_score(roots, faults, typed=True, alias=False, *, prediction_is_standard=False):
    # Legacy callers may supply raw chaos_type values. Formal answers instead
    # already contain paper fault_kind values, which must not be projected again:
    # compact('pod_failure') collides with compact('PodFailure').
    def prediction_kind(root):
        if prediction_is_standard:
            kind = root.get('fault_kind')
            if kind not in GROUPS:
                raise ValueError(f'Unknown standard prediction fault_kind: {kind!r}')
            return kind
        return norm_kind(root.get('chaos_type', root.get('fault_kind')), alias)
    pred = sorted(set((norm_service(r['service']), prediction_kind(r) if typed else '') for r in roots))
    required, optional = [], []
    # The paper grades sets after observable-type projection. Collapse exact duplicate truths.
    seen = set()
    for f in faults:
        key = (f['role'], f['service'], f['kind'] if typed else '')
        if key in seen: continue
        seen.add(key)
        if f['role'] == 'required': required.append(f)
        elif f['role'] == 'optional': optional.append(f)
    def match(pair, f):
        return pair[0] in f['services'] and (not typed or pair[1] == f['kind'])
    assigned = {}
    def augment(i, seen_gold):
        for j, f in enumerate(required):
            if j in seen_gold or not match(pred[i], f): continue
            seen_gold.add(j)
            if j not in assigned or augment(assigned[j], seen_gold):
                assigned[j] = i
                return True
        return False
    for i in range(len(pred)): augment(i, set())
    credited = set(assigned.values())
    ignored = {i for i, p in enumerate(pred) if i not in credited and any(match(p, f) for f in optional)}
    tp, fp, fn = len(credited), len(pred)-len(credited)-len(ignored), len(required)-len(credited)
    precision = tp/(tp+fp) if tp+fp else (1.0 if not required else 0.0)
    recall = tp/len(required) if required else 1.0
    all_hits = {p[0] for p in pred if any(match(p, f) for f in required)}
    return dict(precision=precision, recall=recall,
                f1=2*precision*recall/(precision+recall) if precision+recall else 0.0,
                exact=fp == fn == 0, tp=tp, fp=fp, fn=fn, ignored_optional=len(ignored),
                hit_services=sorted(all_hits))

def reachable(start, alarms, edges, zero=False):
    if zero and start in alarms: return True
    adjacency = defaultdict(list)
    for a, b in edges: adjacency[a].append(b)
    seen, queue = {start}, deque([start])
    while queue:
        a = queue.popleft()
        for b in adjacency[a]:
            if b in seen: continue
            if b in alarms: return True
            seen.add(b)
            queue.append(b)
    return False

def one(case, pred, valid, network=True, alias=False, include_graph_roots=False):
    t = truth(case, network, include_graph_roots)
    roots = pred.get('root_causes', []) if valid else []
    pair = root_score(roots, t['faults'], True, alias)
    service = root_score(roots, t['faults'], False, alias)
    pe = {tuple(map(norm_service, e)) for e in pred.get('service_edges', [])} if valid else set()
    pe = {e for e in pe if not(set(e) & LOAD) and e[0] != e[1]}
    pn = ({norm_service(s) for s in pred.get('service_nodes', [])} |
          {n for e in pe for n in e}) - LOAD if valid else set()
    if valid and include_graph_roots:
        pn |= {norm_service(r['service']) for r in roots} - LOAD
    node, edge = prf(pn, t['nodes']), prf(pe, t['edges'])
    values = dict(em=float(pair['exact']), f1=pair['f1'], precision=pair['precision'], recall=pair['recall'],
                  anysvc=float(bool(service['hit_services'])), node_f1=node['f1'], edge_f1=edge['f1'],
                  root_service_f1=service['f1'], root_service_em=float(service['exact']))
    for suffix, hits in [('', service['hit_services']), ('_typed', pair['hit_services'])]:
        for zero in (False, True):
            values['pr'+suffix+('_zero' if zero else '_positive')] = float(any(
                reachable(s, t['alarms'], pe, zero) for s in hits if s not in LOAD))
    if not valid: values = {k: 0.0 for k in values}
    assert values['pr_positive'] <= values['pr_zero'] <= values['anysvc']
    return dict(case=case, system=t['system'], valid=valid, legal_empty=valid and not roots,
                process_applicable=t['process_applicable'], **values,
                typed_counts={k:pair[k] for k in ('tp','fp','fn','ignored_optional')},
                predicted_roots=roots, predicted_edges=sorted(pe), truth_edges=sorted(t['edges']),
                predicted_nodes=sorted(pn), truth_nodes=sorted(t['nodes']),
                alarm_services=sorted(t['alarms']), service_hits=service['hit_services'],
                added_truth_nodes=t['added_nodes'], network_endpoint_audit=t['endpoint_audit'])

FIELDS = ['em','f1','precision','recall','anysvc','pr_positive','pr_zero','node_f1','edge_f1',
          'pr_typed_positive','pr_typed_zero','root_service_f1','root_service_em']
def aggregate(rows):
    n = len(rows)
    return dict(n=n, valid=sum(x['valid'] for x in rows), failures=sum(not x['valid'] for x in rows),
                legal_empty=sum(x['legal_empty'] for x in rows),
                metrics={k:sum(x[k] for x in rows)/n if n else None for k in FIELDS},
                counts={k:sum(x[k] for x in rows) for k in ['em','anysvc','pr_positive','pr_zero','root_service_em']},
                sql_exec=None, sql_exec_reason='No emitted executable SQL evidence contract')

def load_archive(path):
    data = read(path)
    return {r['case']: {'prediction':r.get('prediction') or {}, 'valid':bool(r['valid_final']),
                       'source':str(path), 'source_kind':'archived_prediction'} for r in data['cases']}

def load_project(name, cases, archival=None, reconstructed=False):
    rows = {}
    for case in cases:
        base = ROOT/'.exp'/name/'runs/OpenRCA2'/('case_'+case)
        path = base/'prediction.json'
        if not path.exists(): continue
        pred = read(path)
        try:
            manifest = read(base/'run_manifest.json') if (base/'run_manifest.json').exists() else {}
        except json.JSONDecodeError:
            # Keep restored responses separate from completed model runs.
            if not reconstructed:
                raise
            manifest = {'status': 'unparseable'}
        valid = pred.get('output_selection_status') != 'technical_failure'
        if archival is not None:
            assert pred == archival[case]['prediction'], (name, case, 'archive differs')
            valid = valid and archival[case]['valid']
        elif not reconstructed:
            valid = valid and manifest.get('status') == 'completed'
        rows[case] = {'prediction': pred, 'valid': valid, 'source':str(path),
                      'source_kind':'reconstructed_response' if reconstructed else 'prediction_file',
                      'manifest_status':manifest.get('status')}
    return rows

def checks():
    assert norm_kind('ContainerKill') == norm_kind('PodKill') != norm_kind('PodFailure')
    assert norm_kind('StressCPU') != norm_kind('CPUStress')
    assert norm_kind('StressCPU', True) == norm_kind('CPUStress')
    assert norm_service('ts-User_Service') == 'userservice'
    assert prf([], [])['f1'] == 1
    assert not reachable('a', {'a'}, {('a','b'),('b','a')}, False)
    assert reachable('a', {'a'}, set(), True)
    assert reachable('a', {'c'}, {('a','b'),('b','c')})
    f=[{'role':'required','service':'a','services':['a','b'],'kind':'network_delay'},
       {'role':'required','service':'c','services':['b','c'],'kind':'network_delay'}]
    assert root_score([{'service':'b','chaos_type':'NetworkDelay'}],f)['tp'] == 1
    assert root_score([{'service':'a','chaos_type':'NetworkDelay'}, {'service':'b','chaos_type':'NetworkDelay'}],f)['tp'] == 2
    f=[{'role':'required','service':'a','services':['a'],'kind':'cpu_stress'},
       {'role':'optional','service':'b','services':['b'],'kind':'mem_stress'}]
    assert root_score([{'service':'b','chaos_type':'MemoryStress'}],f)['tp'] == 0

def main():
    checks()
    cases = read(ROOT/'reports/round7_20261016/case_selection.json')['all_cases']
    assert len(cases) == len(set(cases)) == 70
    versions = {}
    for name in ('r7r','r7c'):
        ar = load_archive(ROOT/'reports/round7_20261016'/f'{name}_evaluation.json')
        versions[name] = load_project(name, cases, ar)
    versions['r7base_t0'] = load_project('r7base_t0', cases)
    versions['r7workbuddy-1'] = load_project('r7workbuddy-1', cases)
    versions['r7workbuddy-2_reconstructed'] = load_project('r7workbuddy-2', cases, reconstructed=True)
    for arm in ('reference','materials','all_chains','time_boundary','output_reliability'):
        versions['r7x_'+arm] = load_archive(ROOT/'reports/r7x_20261017/regression70'/arm/'evaluation.json')
    for arm in ('root_selection','materials_lossless'):
        versions['r7y_'+arm+'_first70'] = load_archive(ROOT/'reports/r7y_20261017/first70_continuation'/f'{arm}_evaluation.json')
    for arm in ('r8r','r8c'):
        versions[arm] = load_archive(ROOT/'reports/round8_20261016/regression70'/f'{arm}_evaluation.json')
    result = {'paper':'https://arxiv.org/html/2606.27154v2','case_count':70,
              'policies':{'main':'paper type mapping + explicit network endpoints + local label roles',
                          'failures':'zero on all metrics, retained denominator',
                          'process':'all70 paper denominator and eligible66 local denominator both reported',
                          'pr':'predicted directed graph, service hit anchors; positive and zero-hop shown',
                          'type_aliases':'StressCPU/StressMemory excluded from primary; separate sensitivity',
                          'nodes':'explicit graph nodes + edge endpoints; no forced root insertion; loadgen removed',
                          'normalization':'lowercase, strip leading ts-, remove hyphens and underscores (G.2/F.3)',
                          'sql':'N/A; no output evidence.sql, never substituted with evidence ID coverage'},
              'variants':{}}
    details, sensitivity = {}, {}
    for name, records in versions.items():
        assert set(records) == set(cases), (name, len(records), set(cases)-set(records))
        rows = [one(c, records[c]['prediction'], records[c]['valid']) for c in cases]
        details[name] = rows
        result['variants'][name] = {'all70':aggregate(rows),
            'process_eligible':aggregate([r for r in rows if r['process_applicable']]),
            'by_system':{s:aggregate([r for r in rows if r['system']==s]) for s in ['ts','hs','otel-demo']},
            'source_kind': sorted({r['source_kind'] for r in records.values()}),
            'source_files': sorted({r['source'] for r in records.values()}),
            'comparison_status':'response_reconstruction_not_full_run' if 'reconstructed' in name else 'same70_preserved_run'}
        if name == 'r7r':
            for key, network, alias, add_roots in [('explicit_labels_only',False,False,False),
                                                  ('add_project_type_aliases',True,True,False),
                                                  ('add_all_graph_roots_to_nodes',True,False,True)]:
                rr=[one(c,records[c]['prediction'],records[c]['valid'],network,alias,add_roots) for c in cases]
                sensitivity[key]={'all70':aggregate(rr),'process_eligible':aggregate([r for r in rr if r['process_applicable']]),
                                  'changed_cases':[r['case'] for r,b in zip(rr,rows) if any(r[f]!=b[f] for f in FIELDS)]}
    result['r7r_sensitivity'] = sensitivity
    result['checks']={'synthetic_contract_checks_passed':True,'all_variants_same70':True,'source_files_hashed':len(HASHES)}
    save('paper_metrics.json',result)
    save('per_case_metrics.json',details)
    save('paper_type_mapping.json',GROUPS)
    save('scoring_input_sha256.json',HASHES)
    with (OUT/'comparison_metrics.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['variant','n','valid','failures','legal_empty',*FIELDS,'process_n','node_f1_eligible','edge_f1_eligible','pr_positive_eligible'])
        w.writeheader()
        for name, v in result['variants'].items():
            a,b=v['all70'],v['process_eligible']
            w.writerow({'variant':name,**{k:a[k] for k in ['n','valid','failures','legal_empty']},**a['metrics'],
                        'process_n':b['n'],'node_f1_eligible':b['metrics']['node_f1'],'edge_f1_eligible':b['metrics']['edge_f1'],
                        'pr_positive_eligible':b['metrics']['pr_positive']})
    print(json.dumps({n:{'valid':v['all70']['valid'],'failures':v['all70']['failures'],**v['all70']['metrics']} for n,v in result['variants'].items()},ensure_ascii=False,indent=2))

if __name__ == '__main__':
    main()
