function tok(){ return ''; }
function canManageScheduling(){ return ['super_admin','tenant_admin','scheduler'].includes(localStorage.getItem('user_role')||''); }
function rulesTenantSelector(){
  return {
    tenantId: new URLSearchParams(location.search).get('tenant_id') || '',
    tenants: [],
    isSuper: localStorage.getItem('user_role') === 'super_admin',
    get tenantName(){ return this.tenants.find(tenant => tenant.id === this.tenantId)?.name || ''; },
    async load(){
      if(localStorage.getItem('user_role') !== 'super_admin') return;
      const r=await fetch('/api/v1/tenants?page_size=100',{headers:{Authorization:tok()}});
      if(r.ok) this.tenants=(await r.json()).items;
    },
    reload(){ location.search=this.tenantId ? '?tenant_id='+encodeURIComponent(this.tenantId) : ''; },
  };
}
function sm(){
  let requirementKey = 0;
  function newRequirement(){ requirementKey += 1; return {key: requirementKey, role_id:'', skill_id:'', count:1}; }
  return {
  isSuper: localStorage.getItem('user_role') === 'super_admin',
  get canManage(){ return canManageScheduling(); },
  items:[], shifts:[], roles:[], skills:[], loading:true, show:false,
  tenantId: new URLSearchParams(location.search).get('tenant_id') || '',
  total:0, page:0, pageSize:50, loadingMore:false,
  sortKey:'name', sortDir:1,
  form:{name:'',shift_template_id:'',priority:0,requirements:[]},
  addRequirement(){ this.form.requirements.push(newRequirement()); },
  sortBy(k){ if(this.sortKey===k){ this.sortDir=-this.sortDir; } else { this.sortKey=k; this.sortDir=1; } },
  sortArrow(k){ return this.sortKey===k ? (this.sortDir>0?'▲':'▼') : ''; },
  sortedItems(){ const a=[...this.items]; const d=this.sortDir; if(this.sortKey==='name'){ a.sort((x,y)=>x.name.localeCompare(y.name)*d); } else if(this.sortKey==='shift'){ a.sort((x,y)=>((x.shift_template_code||'')+(x.shift_template_name||'')).localeCompare((y.shift_template_code||'')+(y.shift_template_name||''))*d); } return a; },
  get hasMore(){ return this.items.length < this.total; },
  get canCreateTarget(){ return !this.isSuper || Boolean(this.tenantId); },
  tenantQuery(){ return this.tenantId ? '?tenant_id='+encodeURIComponent(this.tenantId) : ''; },
  get availableRoles(){ return this.tenantId ? this.roles.filter(r=>r.tenant_id===this.tenantId) : this.roles; },
  get availableSkills(){ return this.tenantId ? this.skills.filter(s=>s.tenant_id===this.tenantId) : this.skills; },
  get availableShifts(){ return this.tenantId ? this.shifts.filter(s=>s.tenant_id===this.tenantId) : this.shifts; },
  async load(reset=true){
    this.loading = true;
    try {
      await this.loadRules(reset);
    } finally {
      this.loading = false;
    }
  },
  async loadRules(reset=false){
    const page = reset ? 1 : this.page + 1;
    if (!reset) this.loadingMore = true;
    try {
      const query=this.tenantQuery() ? this.tenantQuery()+'&' : '?';
      const r = await fetch(`/api/v1/skill-mix-rules${query}page=${page}&page_size=${this.pageSize}`, {headers:{Authorization:tok()}});
      if (!r.ok) return;
      const data = await r.json();
      this.items = reset ? data.items : this.items.concat(data.items);
      this.total = data.total ?? this.items.length;
      this.page = data.page ?? page;
    } finally {
      if (!reset) this.loadingMore = false;
    }
  },
  async loadMore(){ if (!this.loadingMore && this.hasMore) await this.loadRules(false); },
  async openForm(){ this.show=true; this.form={name:'',shift_template_id:'',priority:0,requirements:[]}; await this._loadOpts(); },
  async _loadOpts(){ if(!this.shifts.length){const s=await fetch('/api/v1/shift-templates?page_size=100',{headers:{Authorization:tok()}}); if(s.ok) this.shifts=(await s.json()).items;} await this.loadRoles(); await this.loadSkills(); },
  async edit(r){ this.show=true; await this._loadOpts(); this.form={id:r.id,name:r.name,shift_template_id:r.shift_template_id,priority:r.priority,requirements:(r.requirements||[]).map(q=>{const requirement=newRequirement();requirement.role_id=q.role_id||'';requirement.skill_id=q.skill_id||'';requirement.count=q.count;return requirement;})}; await this.$nextTick(); this.form={...this.form}; },
  async loadRoles(){ if(this.roles.length) return; const r=await fetch('/api/v1/roles?page_size=100',{headers:{Authorization:tok()}}); if(r.ok) this.roles=(await r.json()).items; },
  async loadSkills(){ if(this.skills.length) return; const r=await fetch('/api/v1/skills?page_size=100',{headers:{Authorization:tok()}}); if(r.ok) this.skills=(await r.json()).items; },
  cancel(){ this.show=false; this.form={name:'',shift_template_id:'',priority:0,requirements:[]}; },
  async submit(){ const reqs=this.form.requirements.filter(q=>q.count>=0).map(q=>({role_id:q.role_id||null,skill_id:q.skill_id||null,count:q.count})); const body=JSON.stringify({name:this.form.name,shift_template_id:this.form.shift_template_id,priority:this.form.priority,requirements:reqs}); let r; if(this.form.id){ r=await fetch('/api/v1/skill-mix-rules/'+this.form.id,{method:'PATCH',headers:{'Content-Type':'application/json',Authorization:tok()},body}); } else { r=await fetch('/api/v1/skill-mix-rules'+this.tenantQuery(),{method:'POST',headers:{'Content-Type':'application/json',Authorization:tok()},body}); } if(r.ok){this.cancel(); await this.load();} else alert((await r.text())); },
  async del(id){ if(!confirm('删除?')) return; const r=await fetch('/api/v1/skill-mix-rules/'+id,{method:'DELETE',headers:{Authorization:tok()}}); if(!r.ok){const t=await r.text(); alert(t);} await this.load(); },
}}
function seq(){
  let stepKey = 0;
  function newStep(){ stepKey += 1; return {key: stepKey, shift_template_id:''}; }
  return {
  isSuper: localStorage.getItem('user_role') === 'super_admin',
  get canManage(){ return canManageScheduling(); },
  items:[], shifts:[], roles:[], loading:true, show:false,
  tenantId: new URLSearchParams(location.search).get('tenant_id') || '',
  total:0, page:0, pageSize:50, loadingMore:false,
  sortKey:'name', sortDir:1,
  form:{name:'',description:'',steps:[newStep()],role_ids:[]},
  addStep(){ this.form.steps.push(newStep()); },
  sortBy(k){ if(this.sortKey===k){ this.sortDir=-this.sortDir; } else { this.sortKey=k; this.sortDir=1; } },
  sortArrow(k){ return this.sortKey===k ? (this.sortDir>0?'▲':'▼') : ''; },
  seqLabel(r){ return (r.steps||[]).sort((a,b)=>a.position-b.position).map(s=>s.shift_template_name?(s.shift_template_code+' '+s.shift_template_name):'休').join(' → '); },
  sortedItems(){ const a=[...this.items]; const d=this.sortDir; if(this.sortKey==='name'){ a.sort((x,y)=>x.name.localeCompare(y.name)*d); } else if(this.sortKey==='seq'){ a.sort((x,y)=>this.seqLabel(x).localeCompare(this.seqLabel(y))*d); } return a; },
  get hasMore(){ return this.items.length < this.total; },
  get canCreateTarget(){ return !this.isSuper || Boolean(this.tenantId); },
  tenantQuery(){ return this.tenantId ? '?tenant_id='+encodeURIComponent(this.tenantId) : ''; },
  get availableRoles(){ return this.tenantId ? this.roles.filter(r=>r.tenant_id===this.tenantId) : this.roles; },
  get availableShifts(){ return this.tenantId ? this.shifts.filter(s=>s.tenant_id===this.tenantId) : this.shifts; },
  async load(reset=true){
    this.loading = true;
    try {
      await this.loadRules(reset);
    } finally {
      this.loading = false;
    }
  },
  async loadRules(reset=false){
    const page = reset ? 1 : this.page + 1;
    if (!reset) this.loadingMore = true;
    try {
      const query=this.tenantQuery() ? this.tenantQuery()+'&' : '?';
      const r = await fetch(`/api/v1/shift-sequence-rules${query}page=${page}&page_size=${this.pageSize}`, {headers:{Authorization:tok()}});
      if (!r.ok) return;
      const data = await r.json();
      this.items = reset ? data.items : this.items.concat(data.items);
      this.total = data.total ?? this.items.length;
      this.page = data.page ?? page;
    } finally {
      if (!reset) this.loadingMore = false;
    }
  },
  async loadMore(){ if (!this.loadingMore && this.hasMore) await this.loadRules(false); },
  async _loadOpts(){ if(!this.shifts.length){const s=await fetch('/api/v1/shift-templates?page_size=100',{headers:{Authorization:tok()}}); if(s.ok) this.shifts=(await s.json()).items;} if(!this.roles.length){const r=await fetch('/api/v1/roles?page_size=100',{headers:{Authorization:tok()}}); if(r.ok) this.roles=(await r.json()).items;} },
  async openForm(){ this.show=true; await this._loadOpts(); this.form={name:'',description:'',steps:[newStep()],role_ids:[]}; },
  async edit(r){ await this._loadOpts(); const steps=(r.steps||[]).sort((a,b)=>a.position-b.position).map(s=>{ const step=newStep(); step.shift_template_id=s.shift_template_id||''; return step; }); this.show=true; this.form={id:r.id,name:r.name,description:r.description||'',steps:steps.length?steps:[newStep()],role_ids:r.role_ids||[]}; await this.$nextTick(); this.form={...this.form}; },
  cancel(){ this.show=false; this.form={name:'',description:'',steps:[newStep()],role_ids:[]}; },
  async submit(){ const steps=this.form.steps.map((s,i)=>({position:i,shift_template_id:s.shift_template_id||null})); const body=JSON.stringify({name:this.form.name,description:this.form.description||null,steps,role_ids:this.form.role_ids}); let r; if(this.form.id){ r=await fetch('/api/v1/shift-sequence-rules/'+this.form.id,{method:'PATCH',headers:{'Content-Type':'application/json',Authorization:tok()},body}); } else { r=await fetch('/api/v1/shift-sequence-rules'+this.tenantQuery(),{method:'POST',headers:{'Content-Type':'application/json',Authorization:tok()},body}); } if(r.ok){this.cancel(); await this.load();} else alert((await r.text())); },
  async del(id){ if(!confirm('删除?')) return; const r=await fetch('/api/v1/shift-sequence-rules/'+id,{method:'DELETE',headers:{Authorization:tok()}}); if(!r.ok){const t=await r.text(); alert(t);} await this.load(); },
}}
