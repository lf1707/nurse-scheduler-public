function tok(){ return ''; }
function detail(){ return {
  // requestId is read in init() from the root element's data-request-id
  // (server-rendered) and stored as a reactive field for use in hrefs/fetches.
 requestId: '',
 loading:true, loaded:false, error:'', sched:{}, dates:[], grid:[],
 hasResult:false, emptyResult:false,
 viewMode:'table',
 calendarMonths:[],
 exportFormat:'csv',
 legend:[],
 legendText:'',
 codeTime:{},
 codeName:{},
 failedMsg:'',
 displayId:'',
 versions:[],
 selectedVersion:null,
 latestVersion:null,
 activeVersion:null,
 editMode:false,
 saving:false,
 editErrors:[],
 originalCells:{},
 needsOverride:false,
 overrideReason:'',
 resolveMode:false,
 resolveSaving:false,
 resolveBaseVersion:null,
 resolveProposal:[],
 resolveOriginal:[],
 pendingResolve:false,
 savingResolve:false,
 resolveSaveError:'',
 deletingVersion:false,
 deleteError:'',
 resolveNeedsOverride:false,
 resolveOverrideReason:'',
 resolvePinned:{},
 resolveDateStart:'',
 resolveDateEnd:'',
 resolveDiff:[],
 resolveError:'',
 init(){
   this.requestId = this.$el.dataset.requestId;
   const version=Number(new URLSearchParams(location.search).get('version'));
   this.selectedVersion=Number.isInteger(version)&&version>0?version:null;
   this.load();
 },
 get isLatestVersion(){
   return !this.latestVersion || this.sched.version===this.latestVersion;
 },
 get isActiveVersion(){
   return !this.activeVersion || this.sched.version===this.activeVersion;
 },
 versionLabelClass(item){
   return [
     'version-chip',
     item.version===this.sched.version?'active':'',
     item.version===this.activeVersion?'effective':'',
   ].filter(Boolean).join(' ');
 },
 versionButtonText(item){
   let text='V'+item.version+' · '+item.assignment_count+' 班次';
   if(item.version===this.sched.version) text+=' · 当前';
   if(item.version===this.activeVersion) text+=' · 生效';
   return text;
 },
 versionOptions(){
   return this.versions.slice().reverse();
 },
 async switchVersion(version){
   if(version===this.sched.version) return;
   this.selectedVersion=version;
   history.replaceState({}, '', `/pages/schedules/${this.requestId}?version=${version}`);
   await this.load();
 },
 async deleteVersion(){
   if(!this.versions || this.versions.length<=1 || this.isActiveVersion) return;
   if(!confirm('确认删除当前浏览的 V'+this.sched.version+' 吗？删除后不再显示。')) return;
   this.deletingVersion=true;
   this.deleteError='';
   try{
     const response=await fetch('/api/v1/schedules/'+this.requestId+'/versions/'+this.sched.version,{
       method:'DELETE',headers:{Authorization:tok()}});
     if(response.ok){
       this.selectedVersion=null;
       history.replaceState({}, '', '/pages/schedules/'+this.requestId);
       await this.load();
       return;
     }
     const data=await response.json().catch(()=>({}));
     this.deleteError=typeof data.detail==='string'?data.detail:('删除失败（HTTP '+response.status+'）');
   }catch(error){
     this.deleteError='网络错误：'+error.message;
   }finally{
     this.deletingVersion=false;
   }
 },
 startEdit(){
   this.editMode=true; this.editErrors=[];
   this.needsOverride=false; this.overrideReason='';
   this.originalCells={};
   for(const row of this.grid){ this.originalCells[row.nurseId]={...row.cells}; }
 },
 cancelEdit(){
   this.editMode=false; this.editErrors=[];
   for(const row of this.grid){ row.cells={...this.originalCells[row.nurseId]}; }
 },
 collectDiff(){
   const ops=[];
   for(const row of this.grid){
     const before=this.originalCells[row.nurseId]||{};
     const after=row.cells;
     for(const d of Object.keys({...before,...after})){
       const old=before[d]||'';
       const cur=after[d]||'';
       if(old===cur) continue;
       if(old && !cur){
         ops.push({action:'remove',nurse_id:row.nurseId,date:d,
           shift_template_id:this.codeToId(old)});
       } else if(old && cur && old!==cur){
         ops.push({action:'remove',nurse_id:row.nurseId,date:d,
           shift_template_id:this.codeToId(old)});
         ops.push({action:'add',nurse_id:row.nurseId,date:d,
           shift_template_id:this.codeToId(cur)});
       } else if(!old && cur){
         ops.push({action:'add',nurse_id:row.nurseId,date:d,
           shift_template_id:this.codeToId(cur)});
       }
     }
   }
   return ops;
 },
 codeToId(code){
   const st=(this.sched.shift_templates||[]).find(b=>b.code===code);
   return st?st.id:code;
 },
 async saveEdit(){
   const ops=this.collectDiff();
   if(!ops.length){ this.editMode=false; return; }
   this.saving=true; this.editErrors=[];
   try{
     const r=await fetch('/api/v1/schedules/'+this.requestId+'/assignments',{
       method:'PATCH',
       headers:{'Content-Type':'application/json',Authorization:tok()},
      body:JSON.stringify({base_version:this.sched.version,operations:ops,
        override_reason:this.needsOverride&&this.overrideReason?this.overrideReason:undefined})
     });
     if(r.ok){
       const data=await r.json();
       this.editMode=false;
       await this.load();
     } else {
       const body=await r.json().catch(()=>({}));
       const details=Array.isArray(body.detail)?body.detail:
         [{code:'error',message:body.detail||'保存失败'}];
       this.editErrors=details.map(d=>({
         code:d.code||'',
         msg:this.formatError(d),
         date:d.date||d.next_date||'',
         nurse_id:d.nurse_id||''
       }));
       this.needsOverride=details.some(d=>d.code==='override_required');
     }
   } catch(e){
     this.editErrors=[{code:'network',msg:'网络错误：'+e.message}];
   } finally{ this.saving=false; }
 },
 openResolve(){
   if(this.pendingResolve) return;
   this.resolveMode=true;
   this.resolveSaving=false;
   this.resolveBaseVersion=null;
   this.resolveProposal=[];
   this.resolveDiff=[];
   this.resolveError='';
   this.resolvePinned={};
   for(const row of this.grid){ this.resolvePinned[row.nurseId]=true; }
   this.resolveDateStart=this.dates[0]||'';
   this.resolveDateEnd=this.dates[this.dates.length-1]||'';
 },
 cancelResolve(){
   this.resolveMode=false;
   this.resolveProposal=[];
   this.resolveBaseVersion=null;
   this.resolveDiff=[];
   this.resolveError='';
 },
 togglePin(nurseId){
   this.resolvePinned[nurseId]=!this.resolvePinned[nurseId];
 },
 get lockedNurseCount(){
   return Object.values(this.resolvePinned).filter(Boolean).length;
 },
 get isFullResolveRange(){
   return this.resolveDateStart===this.dates[0] &&
     this.resolveDateEnd===this.dates[this.dates.length-1];
 },
 get resolvePinDates(){
   if(this.isFullResolveRange || !this.resolveDateStart || !this.resolveDateEnd) return [];
   const dates=[];
   const cursor=new Date(`${this.resolveDateStart}T00:00:00Z`);
   const end=new Date(`${this.resolveDateEnd}T00:00:00Z`);
   while(cursor<=end){
     dates.push(cursor.toISOString().slice(0,10));
     cursor.setUTCDate(cursor.getUTCDate()+1);
   }
   return dates;
 },
 get lockedCellCount(){
   const pinDates=new Set(this.resolvePinDates);
   return this.grid.reduce((total,row)=>{
     if(!this.resolvePinned[row.nurseId]) return total;
     return total+this.dates.reduce((count,date)=>{
       return count+((!pinDates.size || pinDates.has(date)) && row.cells[date] ? 1:0);
     },0);
   },0);
 },
 normalizeResolveDates(){
   const first=this.dates[0]||'';
   const last=this.dates[this.dates.length-1]||'';
   if(!first || !last) return;
   if(this.resolveDateStart<first) this.resolveDateStart=first;
   if(this.resolveDateStart>last) this.resolveDateStart=last;
   if(this.resolveDateEnd<first) this.resolveDateEnd=first;
   if(this.resolveDateEnd>last) this.resolveDateEnd=last;
   if(this.resolveDateStart>this.resolveDateEnd) this.resolveDateEnd=this.resolveDateStart;
 },
 setFullResolveRange(){
   this.resolveDateStart=this.dates[0]||'';
   this.resolveDateEnd=this.dates[this.dates.length-1]||'';
 },
 nurseLabel(nurseId){
   return (this.grid.find(row=>row.nurseId===nurseId)||{}).nurse||nurseId.slice(0,8);
 },
 formatError(item){
   if(typeof item==='string') return item;
   if(item.code==='override_required'&&Array.isArray(item.soft_violations)&&item.soft_violations.length){
     return item.soft_violations.map(violation=>this.formatError(violation)).join('；');
   }
   const parts=[
     item.nurse_id?this.nurseLabel(item.nurse_id):'',
     item.date||item.next_date||'',
     item.shift_template_id?this.shiftLabel(item.shift_template_id):'',
     item.message||item.msg||JSON.stringify(item),
   ];
   return parts.filter(Boolean).join(' · ');
 },
 shiftLabel(assignmentId){
   if(!assignmentId) return '空';
   const template=(this.sched.shift_templates||[]).find(item=>item.id===assignmentId);
   return template?template.code:assignmentId.slice(0,8);
 },
 async runResolve(){
   this.resolveSaving=true;
   this.resolveError='';
   const lockedNurseIds=Object.keys(this.resolvePinned).filter(id=>this.resolvePinned[id]);
   const pinDates=this.resolvePinDates;
   const body={
     base_version:this.sched.version,
     pin_all:lockedNurseIds.length===this.grid.length && !pinDates.length,
     pin_nurse_ids:lockedNurseIds,
   };
   if(pinDates.length){ body.pin_dates=pinDates; }
   try{
     const response=await fetch('/api/v1/schedules/'+this.requestId+'/resolve',{
       method:'POST',
       headers:{'Content-Type':'application/json',Authorization:tok()},
       body:JSON.stringify(body),
     });
     const data=await response.json().catch(()=>({}));
     if(!response.ok){
       let message=data.detail;
       if(Array.isArray(message)){
         message=message.map(item=>this.formatError(item)).join('；');
       }
       if(typeof message!=='string'||!message){
         message=`局部重排失败（HTTP ${response.status}）`;
       }
       this.resolveError=message;
       return;
     }
     this.resolveBaseVersion=data.base_version;
     this.resolveProposal=data.assignments||[];
     this.resolveDiff=data.diff||[];
     if(!this.resolveDiff.length){ this.resolveError='重排完成，未锁范围没有变化，无需保存。'; }
   }catch(error){
     this.resolveError='网络错误：'+error.message;
   }finally{
     this.resolveSaving=false;
   }
 },
 showResolvePreview(){
   if(!this.resolveProposal.length) return;
   this.resolveOriginal=JSON.parse(JSON.stringify(this.grid));
   const codeById={};
   for(const template of this.sched.shift_templates||[]) codeById[template.id]=template.code;
   const cellsByNurse={};
   const assignmentsByDate={};
   this.resolveProposal.forEach((assignment,index)=>{
     const code=codeById[assignment.shift_template_id]||assignment.shift_template_id.slice(0,4);
     if(!cellsByNurse[assignment.nurse_id]) cellsByNurse[assignment.nurse_id]={};
     cellsByNurse[assignment.nurse_id][assignment.date]=code;
     if(!assignmentsByDate[assignment.date]) assignmentsByDate[assignment.date]=[];
     assignmentsByDate[assignment.date].push({...assignment, code, id:'preview-'+index,
       nurse_name:this.nurseLabel(assignment.nurse_id)});
   });
   for(const row of this.grid){ row.cells=cellsByNurse[row.nurseId]||{}; }
   this.calendarMonths=this.buildCalendarMonths(assignmentsByDate);
   this.pendingResolve=true;
   this.resolveMode=false;
   this.resolveSaveError='';
 },
 async discardResolve(){
   this.pendingResolve=false;
   this.resolveProposal=[];
   this.resolveBaseVersion=null;
   this.resolveDiff=[];
   this.resolveError='';
   this.resolveSaveError='';
   this.resolveNeedsOverride=false;
   this.resolveOverrideReason='';
   await this.load();
 },
 async saveResolve(){
   if(!this.resolveProposal.length) return;
   this.savingResolve=true;
   this.resolveSaveError='';
   try{
     const response=await fetch('/api/v1/schedules/'+this.requestId+'/resolve/commit',{
       method:'POST',
       headers:{'Content-Type':'application/json',Authorization:tok()},
       body:JSON.stringify({base_version:this.resolveBaseVersion,
         assignments:this.resolveProposal,
         override_reason:this.resolveNeedsOverride&&this.resolveOverrideReason?this.resolveOverrideReason:undefined}),
     });
     if(response.ok){
       this.resolveNeedsOverride=false;
       this.resolveOverrideReason='';
       await this.discardResolve();
       return;
     }
     const data=await response.json().catch(()=>({}));
     let message=Array.isArray(data.detail)
       ?data.detail.map(item=>this.formatError(item)).join('；')
       :data.detail;
     if(typeof message!=='string'||!message) message='保存失败（HTTP '+response.status+'）';
     this.resolveSaveError=message;
     this.resolveNeedsOverride=Array.isArray(data.detail)&&
       data.detail.some(item=>item.code==='override_required');
   }catch(error){
     this.resolveSaveError='网络错误：'+error.message;
   }finally{
     this.savingResolve=false;
   }
 },
 async download(){
   const version=this.selectedVersion?`&version=${this.selectedVersion}`:'';
   const r=await fetch('/api/v1/schedules/'+this.requestId+'/export?format='+encodeURIComponent(this.exportFormat)+'&view='+encodeURIComponent(this.viewMode)+version,{headers:{Authorization:tok()}});
   if(!r.ok){ alert('导出失败：'+(await r.text())); return; }
   const blob=await r.blob();
   const url=URL.createObjectURL(blob);
   const a=document.createElement('a');
   a.href=url;
   a.download='schedule_'+this.requestId.slice(0,8)+'.'+this.exportFormat;
   document.body.appendChild(a);
   a.click();
   a.remove();
   URL.revokeObjectURL(url);
 },
 async load(){
   this.loading=true;
   // First fetch the request to know period + status.
   const rr=await fetch('/api/v1/schedules/'+this.requestId,{headers:{Authorization:tok()}});
   if(!rr.ok){ this.error='请求不存在或无权访问'; this.loading=false; return; }
   const req=await rr.json();
   if(req.status!=='completed'){ this.sched={period_start:req.period_start,period_days:req.period_days,outcome:req.status,requested_by_name:req.requested_by_name,assignments:null}; this.displayId=req.display_id; this.failedMsg=req.error_message||''; this.hasResult=false; this.loaded=true; this.loading=false; return; }
    // Fetch the solved result.
    const [versionsResponse,resultResponse]=await Promise.all([
      fetch('/api/v1/schedules/'+this.requestId+'/versions',{headers:{Authorization:tok()}}),
      fetch('/api/v1/schedules/'+this.requestId+'/result'+(this.selectedVersion?`?version=${this.selectedVersion}`:''),{headers:{Authorization:tok()}}),
    ]);
    if(versionsResponse.ok){
      this.versions=await versionsResponse.json();
      this.latestVersion=this.versions.length?this.versions[0].version:null;
    }
    this.activeVersion=req.active_version??this.latestVersion??null;
    const r=resultResponse;
    if(!r.ok){ this.error='结果不可用'; this.loading=false; return; }
    this.sched=await r.json();
    this.displayId=req.display_id;
    this.sched.requested_by_name=req.requested_by_name;
    this.buildGrid();
    this.hasResult = Array.isArray(this.sched.assignments) && this.sched.assignments.length>0;
    this.emptyResult = !this.hasResult;
    this.loaded=true; this.loading=false;
  },
  buildGrid(){
    const [startYear, startMonth, startDay]=this.sched.period_start.split('-').map(Number);
    const start=new Date(startYear, startMonth-1, startDay);
    const days=this.sched.period_days;
    this.dates = Array.from({length:days},(_,i)=>{
      const d=new Date(start);
      d.setDate(d.getDate()+i);
      return [
        d.getFullYear(),
        String(d.getMonth()+1).padStart(2,'0'),
        String(d.getDate()).padStart(2,'0'),
      ].join('-');
    });
    const nurseMap={};
    const assignmentsByDate={};
    for(const a of this.sched.assignments){
      if(!nurseMap[a.nurse_id]) nurseMap[a.nurse_id]={nurseId:a.nurse_id,nurse:a.nurse_name||a.nurse_id.slice(0,8),cells:{}};
      const code=a.shift_template_code||(a.shift_template_id||'').slice(0,4);
      nurseMap[a.nurse_id].cells[a.date]=code;
      if(!assignmentsByDate[a.date]) assignmentsByDate[a.date]=[];
      assignmentsByDate[a.date].push({...a, code});
    }
    this.grid=Object.values(nurseMap);
    this.buildLegend();
    this.calendarMonths=this.buildCalendarMonths(assignmentsByDate);
  },
  buildCalendarMonths(assignmentsByDate){
    const months=[];
    const showNurse=this.grid.length>1;
    const [startYear,startMonth,startDay]=this.dates[0].split('-').map(Number);
    const [endYear,endMonth,endDay]=this.dates[this.dates.length-1].split('-').map(Number);
    const periodStart=new Date(startYear,startMonth-1,startDay);
    const periodEnd=new Date(endYear,endMonth-1,endDay);
    const today=this.todayISO();
    let cursor=new Date(startYear,startMonth-1,1);

    while(cursor<=periodEnd){
      const monthStart=new Date(cursor.getFullYear(),cursor.getMonth(),1);
      const gridStart=new Date(monthStart);
      gridStart.setDate(monthStart.getDate()-monthStart.getDay());
      const cells=Array.from({length:42},(_,index)=>{
        const date=new Date(gridStart);
        date.setDate(gridStart.getDate()+index);
        const dateText=this.formatDate(date);
        const outsideMonth=date.getMonth()!==monthStart.getMonth();
        const outsidePeriod=date<periodStart || date>periodEnd;
        const assignments=(outsideMonth || outsidePeriod) ? [] : assignmentsByDate[dateText]||[];
        return {
          date:dateText,
          day:String(date.getDate()).padStart(2,'0'),
          outsideMonth,
          outsidePeriod,
          isToday:dateText===today,
          assignments:assignments.map((assignment,assignmentIndex)=>{
            const name=this.codeName[assignment.code]||assignment.code;
            const time=this.codeTime[assignment.code]||'';
            const nurse=assignment.nurse_name||'护士';
            return {
              id:assignment.id||dateText+'-'+assignment.nurse_id+'-'+assignmentIndex,
              label:showNurse ? nurse+' · '+name : name,
              title:nurse+' · '+name+(time?' · '+time:''),
            };
          }),
        };
      });

      months.push({
        key:this.formatDate(monthStart).slice(0,7),
        title:monthStart.getFullYear()+'年'+(monthStart.getMonth()+1)+'月',
        monthName:(monthStart.getMonth()+1)+'月',
        year:String(monthStart.getFullYear()),
        cells,
      });
      cursor.setMonth(cursor.getMonth()+1);
    }
    return months;
  },
  cellClasses(cell){
    return [
      'calendar-cell',
      cell.isToday ? 'is-today' : '',
      cell.outsideMonth ? 'is-outside-month' : '',
      cell.outsidePeriod ? 'is-outside-period' : '',
    ].filter(Boolean).join(' ');
  },
  formatDate(date){
    return [
      date.getFullYear(),
      String(date.getMonth()+1).padStart(2,'0'),
      String(date.getDate()).padStart(2,'0'),
    ].join('-');
  },
  todayISO(){
    const now=new Date();
    return this.formatDate(now);
  },
  buildLegend(){
    const tpls = Array.isArray(this.sched.shift_templates) ? this.sched.shift_templates : [];
    const ct = {}, cn = {};
    this.legend = tpls.map(t => {
      const time = (t.start_time && t.end_time) ? `${t.start_time.slice(0,5)}-${t.end_time.slice(0,5)}` : '';
      if (t.start_time && t.end_time) ct[t.code] = time;
      cn[t.code] = t.name;
      return { code: t.code, name: t.name, time };
  });
  this.codeTime = ct;
  this.codeName = cn;
  this.legendText = this.legend.length
    ? '日期格内显示班次名称，"·"表示该护士当天无排班。本次排班涉及班次（名称 + 时段）：' + this.legend.map(s => {
      const dayGroup = s.day_group_name ? `，日组 ${s.day_group_name}[${(s.day_numbers || []).join(',')}]` : '';
      return `${s.name}（${s.time || '时段未设置'}${dayGroup}）`;
    }).join('、')
    : '';
},
}}
