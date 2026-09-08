"""Numerical checks for the jitter metrics, no GUI required."""
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import numpy as np
import pandas as pd
from jitter_viewer import Log,discover_imported_logs,metrics,envelope

n=5000
t=np.arange(n)/1000
amplitude=1e-4
frame=pd.DataFrame({'time':t,'actual':.2+amplitude*np.sin(2*np.pi*40*t),
    'previous_target':.2,'target':.2,'status':0x27,'mode':8,'master_op':1,
    'frames_ok':1,'drive_error':0,'cycle':np.arange(n),'command_id':1,'dropped_total':0})
log=Log(Path('synthetic_test'),'synthetic',frame,True)
result=metrics(log,frame)
assert abs(result['hold_centered_rms_rad']-amplitude/np.sqrt(2))<2e-6
assert abs(result['hold_centered_p2p_rad']-2*amplitude)<2e-6
moving=frame.copy();moving['target']=t;moving['previous_target']=t
assert metrics(log,moving)['hold_samples']==0
disabled=frame.copy();disabled['status']=0x21
assert metrics(log,disabled)['hold_samples']==0
broken=frame.copy();broken.loc[2000:,'cycle']+=1
assert metrics(log,broken)['cycle_gaps']==1
huge=np.zeros(100000);huge[54321]=3;huge[65432]=-4
_,reduced=envelope(np.arange(len(huge)),huge)
assert np.max(reduced)==3 and np.min(reduced)==-4
with TemporaryDirectory() as temporary:
    root=Path(temporary)
    for index,backend in enumerate(('ecmaster','soem','igh')):
        folder=root/backend;folder.mkdir()
        old=folder/f'cycle_{backend}_old.csv';new=folder/f'cycle_{backend}_new.csv'
        old.write_text('old');new.write_text('new')
        os.utime(old,ns=(1_000_000_000+index,1_000_000_000+index))
        os.utime(new,ns=(2_000_000_000+index,2_000_000_000+index))
    assert [p.name for p in discover_imported_logs(root)]==[
        'cycle_ecmaster_new.csv','cycle_soem_new.csv','cycle_igh_new.csv']
print('PASS: sine RMS/peak-to-peak, moving and disabled exclusion, cycle gaps, peak-preserving plot reduction')
