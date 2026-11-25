"""Check paper raw-label mapping and already-standard predictions without model calls."""
import unittest

import evaluate


class PaperMappingTests(unittest.TestCase):
    def test_all_local_raw_types_and_paper_equivalences(self):
        groups = {
            'pod_unavailable': ['PodFailure'], 'pod_failure': ['PodKill', 'ContainerKill'],
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
            'jvm_jdbc_latency': ['JVMMySQLLatency'],
            'jvm_method_mutated': ['JVMReturn', 'JVMRuntimeMutator'],
            'dns_resolution_failed': ['DNSError', 'DNSChaos'],
            'dns_resolution_wrong': ['DNSRandom'], 'clock_skew': ['TimeSkew', 'TimeChaos'],
        }
        self.assertEqual(groups, evaluate.metrics.GROUPS)
        raw_to_standard = {raw: kind for kind, raws in groups.items() for raw in raws}
        for raw, expected in raw_to_standard.items():
            with self.subTest(raw=raw):
                self.assertEqual(evaluate.metrics.norm_kind(raw), expected)
                gold = [{'role': 'required', 'service': 'a', 'services': {'a'}, 'kind': expected}]
                score = evaluate.metrics.root_score([{'service': 'a', 'fault_kind': expected}], gold,
                                                    prediction_is_standard=True)
                self.assertTrue(score['exact'])

    def test_pod_types_do_not_collapse(self):
        for raw, predicted, expected in [('PodFailure', 'pod_unavailable', True),
                                         ('PodFailure', 'pod_failure', False),
                                         ('PodKill', 'pod_failure', True),
                                         ('ContainerKill', 'pod_failure', True),
                                         ('PodKill', 'pod_unavailable', False)]:
            with self.subTest(raw=raw, prediction=predicted):
                gold = [{'role': 'required', 'service': 'a', 'services': {'a'},
                         'kind': evaluate.metrics.norm_kind(raw)}]
                got = evaluate.metrics.root_score([{'service': 'a', 'fault_kind': predicted}], gold,
                                                  prediction_is_standard=True)
                self.assertEqual(got['exact'], expected)

    def test_original_reference_default_remains_compatible(self):
        gold = [{'role': 'required', 'service': 'a', 'services': {'a'}, 'kind': 'pod_unavailable'}]
        legacy = evaluate.metrics.root_score([{'service': 'a', 'fault_kind': 'pod_failure'}], gold)
        self.assertTrue(legacy['exact'])  # preserved legacy behavior, not the paper contract
        strict = evaluate.metrics.root_score([{'service': 'a', 'fault_kind': 'pod_failure'}], gold,
                                             prediction_is_standard=True)
        self.assertFalse(strict['exact'])

    def test_unknown_standard_type_is_not_silently_converted(self):
        with self.assertRaises(ValueError):
            evaluate.metrics.root_score([{'service': 'a', 'fault_kind': 'PodFailure'}], [],
                                        prediction_is_standard=True)

    def test_scoring_entry_point_keeps_type_semantics(self):
        answer = {'root_causes': [{'service': 'a', 'fault_kind': 'pod_failure'}], 'propagation': []}
        truth = {'faults': [{'role': 'required', 'service': 'a', 'services': {'a'},
                            'kind': 'pod_unavailable'}], 'applicable': True,
                 'alarms': set(), 'nodes': {'a'}, 'edges': set()}
        metrics, _ = evaluate.score_answer(answer, truth)
        self.assertEqual(metrics['EM'], 0)
        self.assertEqual(metrics['AnySvc'], 1)
        truth['faults'][0]['kind'] = 'pod_failure'
        metrics, _ = evaluate.score_answer(answer, truth)
        self.assertEqual(metrics['EM'], 1)


if __name__ == '__main__':
    unittest.main()
