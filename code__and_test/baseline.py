import sys, time, json, resource, collections
sys.path.insert(0,'.')
from utils.config_loader import load_config
from core.mutant_generator import generate_all_mutants
from core.test_pool_builder import build as build_pool
from core.sandbox_executor import build_kill_matrix
from core.ilp_solver import solve
from core.contracts import STATUS_EQUIVALENT
class DummyLLM:
    def call(self, prompt, temperature=0.0): return "[]"
cfg = load_config("config.json", non_interactive=True)
src = open(cfg["project"]["target_file"], encoding="utf-8").read()
t={}; W0=time.perf_counter()
s=time.perf_counter(); mutants=generate_all_mutants(src,cfg["mutation"],verbose=False); t["P1_generate"]=time.perf_counter()-s
s=time.perf_counter(); pool=build_pool(mutants,DummyLLM(),cfg); t["P2_pool"]=time.perf_counter()-s
s=time.perf_counter(); km,orig=build_kill_matrix(src,mutants,pool,cfg,verbose=False); t["P3_matrix"]=time.perf_counter()-s
s=time.perf_counter(); res=solve(km,mutants,pool,verbose=False); t["P4_ilp"]=time.perf_counter()-s
status=collections.Counter(m.status for m in mutants)
golden={"target_file":cfg["project"]["target_file"],"n_mutants":len(mutants),"pool_size":len(pool),
 "kill_matrix_shape":[len(km),len(km[0]) if km else 0],"status_counts":dict(status),
 "killed":res.n_killed,"total":res.n_total,
 "raw_score":round(res.raw_score,2),"adjusted_score":round(res.adjusted_score,2),
 "selected_test_count":len(res.selected_tests),   # FIX: ILPResult has no .min_test_count; it is len(selected_tests) (see core/reporter.py:123)
 "phase_seconds":{k:round(v,2) for k,v in t.items()},
 "pipeline_wall_seconds":round(time.perf_counter()-W0,2),
 "peak_rss_mb_parent":round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,1),
 "peak_rss_mb_largest_child":round(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss/1024,1),
 "equivalent_detected":status.get(STATUS_EQUIVALENT,0),"llm_mode":"disabled (DummyLLM)"}
open("/tmp/v1_golden_baseline.json","w").write(json.dumps(golden,indent=2,ensure_ascii=False))
print("DONE")
