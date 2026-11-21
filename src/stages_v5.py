"""Bounded, killable process-tree stages. No geometry library in the supervisor."""
import os
import signal
import subprocess
import time

def run_isolated(command, log_path, timeout, cwd=None):
    start=time.perf_counter()
    options={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}
    with log_path.open('w',encoding='utf-8') as log:
        proc=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,cwd=cwd,**options)
        try:
            code=proc.wait(timeout=timeout)
            status='pass' if code==0 else 'error'
        except subprocess.TimeoutExpired:
            status='timeout'
            if os.name=='nt':
                subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW,timeout=10,check=False)
            else:
                try: os.killpg(proc.pid,signal.SIGKILL)
                except ProcessLookupError: pass
            if proc.poll() is None: proc.kill()
            code=proc.wait(timeout=10)
    return {'status':status,'returncode':code,'wall_s':time.perf_counter()-start,'timeout_s':timeout}
