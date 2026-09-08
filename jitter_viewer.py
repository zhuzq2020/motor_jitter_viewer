"""Import WRC master CSV logs and compare position/time. GUI or headless PNG export."""
from __future__ import annotations
import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import numpy as np
import pandas as pd


@dataclass
class Log:
    path: Path
    backend: str
    frame: pd.DataFrame
    cycle_log: bool
    footer_drops: int = 0


def load_log(path, axis=20):
    path = Path(path)
    with path.open(encoding='utf-8-sig') as f:
        first = f.readline()
    match = re.search(r'backend=(\S+)', first)
    backend = match[1] if match else path.parent.name
    columns = pd.read_csv(path, comment='#', nrows=0).columns
    prefix = f'D{axis}_'
    cycle_log = prefix+'actual_rad' in columns
    actual = prefix+('actual_rad' if cycle_log else 'q_rad')
    if actual not in columns:
        raise ValueError(f'{path.name}: no D{axis} position column; import cycle_*.csv or feedback.csv')
    time_column = 'time_s' if cycle_log else 'host_s'
    wanted = {time_column,'cycle','command_id','phase','master_op','frames_ok','dropped_total'}
    wanted.update(c for c in columns if c.startswith(prefix))
    frame = pd.read_csv(path, comment='#', usecols=lambda c: c in wanted,
                        dtype={'phase':'category'}, on_bad_lines='error')
    frame.rename(columns={time_column:'time',actual:'actual',prefix+'target_rad':'target',
        prefix+'previous_target_rad':'previous_target',prefix+'status':'status',
        prefix+'mode':'mode',prefix+'error':'drive_error'}, inplace=True)
    if len(frame)<2:
        raise ValueError(f'{path.name}: fewer than two samples')
    if not np.isfinite(frame['time']).all() or not np.isfinite(frame['actual']).all():
        raise ValueError(f'{path.name}: incomplete/nonfinite rows; copy a completed log')
    if (np.diff(frame['time'])<=0).any():
        raise ValueError(f'{path.name}: timestamps must increase strictly')
    footer_drops = 0
    with path.open('rb') as f:
        f.seek(max(0,path.stat().st_size-4096))
        tail=f.read().decode('utf-8',errors='replace')
        values=re.findall(r'# dropped_total=(\d+)',tail)
        if values:footer_drops=int(values[-1])
    return Log(path, backend, frame, cycle_log, footer_drops)


def select(log, start=None, end=None, align=True, command=None):
    frame=log.frame.copy()
    if command is not None:
        frame=frame.loc[frame['command_id']==command].copy()
    if len(frame)<2:raise ValueError(f'{log.backend}: selected command has too few samples')
    origin=frame['time'].iloc[0]
    if align and 'phase' in frame:
        moving=frame.loc[frame['phase']=='executing','time']
        if len(moving):origin=moving.iloc[0]
    frame['time']-=origin
    if start is not None:frame=frame.loc[frame['time']>=start]
    if end is not None:frame=frame.loc[frame['time']<=end]
    if len(frame)<2:raise ValueError(f'{log.backend}: time window has too few samples')
    return frame.reset_index(drop=True)


def metrics(log, frame):
    time=frame['time'].to_numpy()
    dt=np.diff(time)*1000
    result={'backend':log.backend,'file':str(log.path),'samples':len(frame),
        'duration_s':time[-1]-time[0],'sample_interval_mean_ms':float(np.mean(dt)),
        'sample_interval_p99_ms':float(np.quantile(dt,.99)),
        'sample_interval_max_ms':float(np.max(dt)),
        'sample_interval_std_ms':float(np.std(dt)),
        'position_p2p_rad':float(np.ptp(frame['actual'])),
        'dropped_total':max(log.footer_drops,int(frame['dropped_total'].max()) if 'dropped_total' in frame else 0),
        'cycle_gaps':int(np.sum(np.maximum(np.diff(frame['cycle'])-1,0))) if log.cycle_log else None,
        'hold_samples':0,'hold_segments':0,'hold_centered_rms_rad':None,'hold_centered_p2p_rad':None,
        'tracking_rms_rad':None,'tracking_p2p_rad':None}
    if 'target' not in frame:return result
    target=frame['previous_target'].to_numpy()
    actual=frame['actual'].to_numpy()
    status=frame['status'].fillna(0).to_numpy(dtype=np.uint16)
    good=((status&0x6f)==0x27)
    good &= frame['mode'].to_numpy()==8
    good &= frame['master_op'].to_numpy()==1
    good &= frame['frames_ok'].to_numpy()==1
    good &= frame['drive_error'].to_numpy()==0
    good &= np.isfinite(target)
    error=actual-target
    if good.any():
        result['tracking_rms_rad']=float(np.sqrt(np.mean(error[good]**2)))
        result['tracking_p2p_rad']=float(np.ptp(error[good]))
    # Keep contiguous stationary-target, enabled CSP intervals only. Exclude first
    # 250 ms after each command change, target change, disabled period or sample gap.
    same=np.r_[False,np.abs(np.diff(target))<=1e-9]
    contiguous=np.r_[False,np.diff(time)<=max(np.median(np.diff(time))*2.5,.0025)]
    if 'cycle' in frame:contiguous &= np.r_[False,np.diff(frame['cycle'])==1]
    if 'command_id' in frame:same &= np.r_[False,np.diff(frame['command_id'])==0]
    runs=np.cumsum(~(same & good & np.r_[False,good[:-1]] & contiguous))
    residuals=[]
    boundaries=np.r_[0,np.flatnonzero(np.diff(runs))+1,len(runs)]
    for lo,hi in zip(boundaries[:-1],boundaries[1:]):
        if hi-lo<20:continue
        idx=np.arange(lo,hi)[good[lo:hi]]
        if len(idx)<2:continue
        settled=idx[time[idx]-time[idx[0]]>=.25]
        if len(settled)>=20:
            residual=error[settled]
            residuals.append(residual-np.mean(residual))
    if residuals:
        residual=np.concatenate(residuals)
        result.update(hold_samples=len(residual),hold_segments=len(residuals),
            hold_centered_rms_rad=float(np.sqrt(np.mean(residual**2))),
            hold_centered_p2p_rad=float(np.ptp(residual)))
    return result


def envelope(x,y,limit=16000):
    """Preserve extrema while reducing rendering work, never use plain stride decimation."""
    x=np.asarray(x);y=np.asarray(y)
    if len(x)<=limit:return x,y
    edges=np.linspace(0,len(x),limit//2+1,dtype=int)
    indices=[]
    for lo,hi in zip(edges[:-1],edges[1:]):
        block=y[lo:hi]
        indices.extend([lo+int(np.argmin(block)),lo+int(np.argmax(block))])
    indices=np.unique(indices)
    return x[indices],y[indices]


def draw(figure, logs, axis=20, start=None, end=None, align=True, command=None, centered=False):
    figure.clear()
    panels=figure.subplots(3,1,sharex=True)
    results=[]
    colors=['#2563eb','#ef4444','#16a34a','#9333ea','#d97706']
    for n,log in enumerate(logs):
        frame=select(log,start,end,align,command)
        result=metrics(log,frame);results.append(result)
        color=colors[n%len(colors)]
        label=f'{n+1}: {log.backend}'
        time=frame['time'].to_numpy();actual=frame['actual'].to_numpy()
        offset=float(np.mean(actual)) if centered else 0.0
        panels[0].plot(*envelope(time,actual-offset),color=color,lw=.8,label=label+' actual')
        if 'target' in frame:
            panels[0].plot(*envelope(time,frame['target'].to_numpy()-offset),color=color,lw=.8,ls='--',alpha=.7,label=label+' target')
            panels[1].plot(*envelope(time,actual-frame['previous_target'].to_numpy()),color=color,lw=.7,label=label)
        else:
            panels[1].plot(*envelope(time,actual-np.mean(actual)),color=color,lw=.7,label=label+' centered position (no target)')
        panels[2].plot(*envelope(time[1:],np.diff(time)*1000),color=color,lw=.7,label=label)
    panels[0].set_ylabel('Position - mean (rad)' if centered else 'Position (rad)')
    panels[1].set_ylabel('Actual - prior target (rad)')
    panels[2].set_ylabel('Sample interval (ms)')
    panels[2].set_xlabel('Time from first executing sample (s)' if align else 'Time from selected log start (s)')
    panels[0].set_title(f'D{axis} | Position / time comparison')
    for panel in panels:panel.grid(True,alpha=.25);panel.legend(loc='upper right',fontsize=8)
    figure.tight_layout()
    return pd.DataFrame(results)


def gui(paths):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg,NavigationToolbar2Tk
    root=tk.Tk();root.title('三种主站 · 电机位置与抖动对比');root.geometry('1350x950')
    controls=ttk.Frame(root,padding=6);controls.pack(fill='x')
    axis=tk.StringVar(value='D20');start=tk.StringVar();end=tk.StringVar();command=tk.StringVar()
    align=tk.BooleanVar(value=True);centered=tk.BooleanVar(value=False)
    selected=[Path(p) for p in paths];cache={};report=None
    figure=Figure(figsize=(12,7),dpi=100)
    canvas=FigureCanvasTkAgg(figure,master=root)
    summary=tk.Text(root,height=8,font=('Consolas',10),wrap='none')
    def render():
        nonlocal report
        try:
            if not selected:raise ValueError('请导入 cycle_*.csv；也支持已有 demo 的 feedback.csv。')
            joint=int(axis.get()[1:]);logs=[]
            for path in selected:
                key=(str(path),joint,path.stat().st_mtime_ns)
                if key not in cache:cache[key]=load_log(path,joint)
                logs.append(cache[key])
            report=draw(figure,logs,joint,float(start.get()) if start.get() else None,
                float(end.get()) if end.get() else None,align.get(),int(command.get()) if command.get() else None,centered.get())
            canvas.draw();summary.delete('1.0','end')
            for i,r in report.iterrows():
                rms='无合格静止段' if pd.isna(r.hold_centered_rms_rad) else f'{r.hold_centered_rms_rad:.6g} rad'
                p2p='N/A' if pd.isna(r.hold_centered_p2p_rad) else f'{r.hold_centered_p2p_rad:.6g} rad'
                summary.insert('end',f'{i+1}. {r.backend}: 静止RMS={rms}, 静止峰峰值={p2p}, 静止样本={r.hold_samples}; 间隔均值={r.sample_interval_mean_ms:.4f} ms, P99={r.sample_interval_p99_ms:.4f} ms, 丢弃={r.dropped_total}\n   {r.file}\n')
            summary.insert('end','静止指标仅取已使能 CSP、目标不变且稳定 ≥250 ms 的连续段，逐段去均值；误差图含伺服响应延迟。\n单看运动中的位置起伏不能判定机械抖动；50 Hz IPC 日志不能测量 1 kHz 控制周期抖动。')
        except Exception as e:messagebox.showerror('读取/绘图失败',str(e))
    def choose():
        files=filedialog.askopenfilenames(title='选择一种或多种主站的 CSV',filetypes=[('CSV','*.csv')])
        if files:selected[:]=map(Path,files);cache.clear();render()
    def export():
        if report is None:return
        name=filedialog.asksaveasfilename(defaultextension='.png',filetypes=[('PNG','*.png')])
        if name:figure.savefig(name,dpi=160);report.to_csv(Path(name).with_suffix('.metrics.csv'),index=False,encoding='utf-8-sig')
    ttk.Button(controls,text='导入日志（多选）',command=choose).pack(side='left',padx=3)
    ttk.Combobox(controls,textvariable=axis,values=[f'D{i}' for i in range(18,25)],width=5,state='readonly').pack(side='left')
    for label,var in [('开始秒',start),('结束秒',end),('命令ID',command)]:
        ttk.Label(controls,text=label).pack(side='left',padx=3);ttk.Entry(controls,textvariable=var,width=7).pack(side='left')
    ttk.Checkbutton(controls,text='首个运动对齐',variable=align).pack(side='left')
    ttk.Checkbutton(controls,text='位置去均值放大',variable=centered).pack(side='left')
    ttk.Button(controls,text='绘图',command=render).pack(side='left',padx=3)
    ttk.Button(controls,text='导出图和指标',command=export).pack(side='left',padx=3)
    canvas.get_tk_widget().pack(fill='both',expand=True)
    NavigationToolbar2Tk(canvas,root).update()
    summary.pack(fill='x',padx=6,pady=5)
    if selected:root.after(50,render)
    root.mainloop()


def discover_imported_logs(root=None):
    """Select the newest non-empty cycle log from each backend directory."""
    root=Path(root) if root else Path(__file__).resolve().parent/'imported_logs'
    selected=[]
    for backend in ('ecmaster','soem','igh'):
        folder=root/backend
        candidates=[p for p in folder.glob('cycle_*.csv')
                    if p.is_file() and p.stat().st_size>0]
        if candidates:
            selected.append(max(candidates,key=lambda p:p.stat().st_mtime_ns))
    return selected


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('logs',nargs='*')
    parser.add_argument('--axis',type=int,choices=range(18,25),default=20)
    parser.add_argument('--start',type=float)
    parser.add_argument('--end',type=float)
    parser.add_argument('--command',type=int)
    parser.add_argument('--no-align',action='store_true')
    parser.add_argument('--centered',action='store_true')
    parser.add_argument('--save',type=Path,help='Headless PNG export; also saves metrics CSV')
    args=parser.parse_args()
    if args.save:
        if not args.logs:parser.error('--save requires one or more CSV logs')
        import matplotlib
        matplotlib.use('Agg')
        from matplotlib.figure import Figure
        figure=Figure(figsize=(13,9),dpi=130)
        report=draw(figure,[load_log(p,args.axis) for p in args.logs],args.axis,args.start,args.end,
            not args.no_align,args.command,args.centered)
        args.save.parent.mkdir(parents=True,exist_ok=True)
        figure.savefig(args.save)
        report.to_csv(args.save.with_suffix('.metrics.csv'),index=False,encoding='utf-8-sig')
        print(report.to_string(index=False))
    else:gui(args.logs if args.logs else discover_imported_logs())


if __name__=='__main__':main()
