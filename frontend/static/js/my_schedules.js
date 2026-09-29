function tok(){ return ''; }
function mySchedules(section='all'){ return {
  scheduleBatches: [], preferences: [], shiftTemplates: [], loading: true, error: '',
  isNurse: false, nurseName: '', preferenceMessage: '',
  showSchedules: section !== 'preferences',
  showPreferences: section !== 'schedules',
  preferenceForm: {date:'', shift_template_id:'', request_type:'like', priority:1},
  async load(){
    this.loading=true; this.error='';
    try {
      const me=await fetch('/api/v1/auth/me',{headers:{Authorization:tok()}});
      if(!me.ok) throw new Error(await this.message(me, '个人信息加载失败'));
      const user=await me.json();
      this.isNurse=user.role==='nurse';
      this.nurseName=user.nurse_name || '';

      if(this.showPreferences) {
        const [templateResponse, preferences]=await Promise.all([
          fetch('/api/v1/shift-templates?page_size=100',{headers:{Authorization:tok()}}),
          fetch('/api/v1/auth/me/preferences',{headers:{Authorization:tok()}}),
        ]);
        if(templateResponse.ok) this.shiftTemplates=(await templateResponse.json()).items;
        if(preferences.ok) this.preferences=(await preferences.json());
      }

      if(this.showSchedules) {
        const endpoint=this.isNurse ? '/api/v1/schedules/mine?page_size=100' : '/api/v1/schedules?page_size=100';
        const list=await fetch(endpoint,{headers:{Authorization:tok()}});
        if(!list.ok) throw new Error(await this.message(list, '排班记录加载失败'));
        this.scheduleBatches=(await list.json()).items
          .filter(item => item.status==='completed')
          .map(item => ({
            request_id: item.id,
            display_id: item.display_id,
            period_start: item.period_start,
            period_end: this.endDate(item.period_start, item.period_days),
            period_days: item.period_days,
            assignment_count: item.assignment_count || 0,
          }))
          .sort((a,b) => b.period_start.localeCompare(a.period_start));
      }
      this.preferenceMessage='';
    } catch(ex) { this.error=ex.message; }
    this.loading=false;
  },
  endDate(start, days){
    const [year, month, day]=start.split('-').map(Number);
    const date=new Date(year, month-1, day+Number(days)-1);
    return [
      date.getFullYear(),
      String(date.getMonth()+1).padStart(2,'0'),
      String(date.getDate()).padStart(2,'0'),
    ].join('-');
  },
  shiftLabel(shiftId){
    const template=this.shiftTemplates.find(item => item.id===shiftId);
    return template ? template.code+' · '+template.name : shiftId;
  },
  async savePreference(){
    this.preferenceMessage='';
    const r=await fetch('/api/v1/auth/me/preferences',{
      method:'POST', headers:{'Content-Type':'application/json',Authorization:tok()},
      body:JSON.stringify(this.preferenceForm),
    });
    if(!r.ok){ this.preferenceMessage='保存失败：'+(await this.message(r,'请稍后再试')); return; }
    await this.loadPreferences();
    this.preferenceMessage='排班期望已保存';
  },
  async loadPreferences(){
    const r=await fetch('/api/v1/auth/me/preferences',{headers:{Authorization:tok()}});
    if(r.ok) this.preferences=await r.json();
  },
  async removePreference(id){
    const r=await fetch('/api/v1/auth/me/preferences/'+id,{method:'DELETE',headers:{Authorization:tok()}});
    if(r.ok) await this.loadPreferences();
  },
  async message(response, fallback){
    const text=await response.text().catch(() => '');
    try { return JSON.parse(text).detail || fallback; } catch(_) { return text || fallback; }
  },
}}
