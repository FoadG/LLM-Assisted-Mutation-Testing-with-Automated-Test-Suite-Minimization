import sys,time,resource; sys.path.insert(0,'.')
from utils.config_loader import load_config
from core.mutant_generator import generate_all_mutants
from core.sandbox_executor import build_kill_matrix
def main():
    cfg=load_config("config.json",non_interactive=True)
    src=open(cfg["project"]["target_file"],encoding="utf-8").read()
    muts=generate_all_mutants(src,cfg["mutation"])[:8]   # subset of mutants
    pool=[{"function":"bubble_sort","inputs":{"arr":[3,1,2]}},
          {"function":"bubble_sort","inputs":{"arr":[]}},
          {"function":"binary_search","inputs":{"arr":[1,2,3],"target":2}}]
    t=time.perf_counter()
    km,_=build_kill_matrix(src,muts,pool,cfg,verbose=False)
    dt=time.perf_counter()-t
    execs=len(km)*len(km[0])
    print(f"GUARD_OK execs={execs} secs={dt:.2f} per_exec_ms={1000*dt/execs:.1f}")
    print(f"peak_rss_parent_mb={resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024:.1f}")
    print(f"peak_rss_child_mb={resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss/1024:.1f}")
if __name__=="__main__":
    main()
