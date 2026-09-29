function tok(){ return ''; }
function canManageScheduling(){ return ['super_admin','tenant_admin','scheduler'].includes(localStorage.getItem('user_role')||''); }
function shiftsPage(){ return {
  items:[], dayGroups:[], tenants:[], loading:true, showForm:false,
  total:0, page:0, pageSize:50, loadingMore:false,
  isSuper: localStorage.getItem('user_role') === 'super_admin',
  get canManage(){ return canManageScheduling(); },
  sortBy:'code', sortDir:'asc',
  form:{code:'',name:'',start_time:'07:00',end_time:'15:00',duration_hours:8.0,day_group_id:''},
  editingId:null,
  get dayGroupNames(){
    const map = {};
    this.dayGroups.forEach(d => map[d.id] = d.name);
    return map;
  },
  get sortedItems(){
    const key = this.sortBy, dir = this.sortDir === 'asc' ? 1 : -1;
    const cmp = (a, b) => {
      let av, bv;
      if (key === 'name') { av = a.name; bv = b.name; }
      else if (key === 'timespan') { av = (a.start_time||''); bv = (b.start_time||''); }
      else if (key === 'dayGroup') { av = this.dayGroupNames[a.day_group_id] || ''; bv = this.dayGroupNames[b.day_group_id] || ''; }
      else { av = a[key] ?? ''; bv = b[key] ?? ''; }
      if (av < bv) return -1;
      if (av > bv) return 1;
      return 0;
    };
    return this.items.slice().sort((x, y) => dir * cmp(x, y));
  },
  tenantId: new URLSearchParams(location.search).get('tenant_id') || '',
  get tenantName(){ return this.tenants.find(tenant => tenant.id === this.tenantId)?.name || ''; },
  get hasMore(){ return this.items.length < this.total; },
  get canCreateTarget(){ return !this.isSuper || Boolean(this.tenantId); },
  tenantQuery(){ return this.tenantId ? '?tenant_id='+encodeURIComponent(this.tenantId) : ''; },
  async load(){
    this.loading=true;
    if(this.isSuper) {
      const r=await fetch('/api/v1/tenants?page_size=100',{headers:{Authorization:tok()}});
      if(r.ok) this.tenants=(await r.json()).items;
    }
    await this.loadDayGroups(true); await this.loadShifts(true); this.loading=false;
  },
  async reloadForTenant(){ location.search=this.tenantId ? '?tenant_id='+encodeURIComponent(this.tenantId) : ''; },
  async toggleForm(){
    this.showForm=!this.showForm; this.editingId=null;
    if(this.showForm) await this.loadDayGroups(true);
  },
  async loadShifts(reset=false){
    const page = reset ? 1 : this.page + 1;
    if (!reset) this.loadingMore=true;
    try {
      const separator=this.tenantQuery() ? (this.tenantQuery().includes('?') ? '&' : '?') : '?';
      const r=await fetch(`/api/v1/shift-templates${this.tenantQuery()}${separator}page=${page}&page_size=${this.pageSize}`,{headers:{Authorization:tok()}}); if(r.ok) {
        const data=await r.json();
        this.items=reset ? data.items : this.items.concat(data.items);
        this.total=data.total ?? this.items.length;
        this.page=data.page ?? page;
      }
    } finally { if(!reset) this.loadingMore=false; }
  },
  async loadMore(){ if(!this.loadingMore && this.hasMore) await this.loadShifts(false); },
  async loadDayGroups(reset=false){
    if(!reset && this.dayGroups.length) return;
    const query=this.tenantQuery() ? this.tenantQuery()+'&' : '?';
    const r=await fetch(`/api/v1/day-groups${query}page_size=100`,{headers:{Authorization:tok()}});
    if(r.ok) this.dayGroups=(await r.json()).items;
  },
  async create(){ const r=await fetch(`/api/v1/shift-templates${this.tenantQuery()}`,{method:'POST',headers:{'Content-Type':'application/json',Authorization:tok()},body:JSON.stringify(this.form)}); if(r.ok){this.showForm=false; await this.load();} else alert((await r.text())); },
  async edit(s){ this.editingId=s.id; await this.loadDayGroups(); this.form={code:s.code,name:s.name,start_time:s.start_time,end_time:s.end_time,duration_hours:s.duration_hours,day_group_id:s.day_group_id}; this.showForm=true; },
  async submit(){ if(this.editingId){ const r=await fetch('/api/v1/shift-templates/'+this.editingId,{method:'PATCH',headers:{'Content-Type':'application/json',Authorization:tok()},body:JSON.stringify(this.form)}); if(r.ok){this.closeForm(); await this.load();} else alert((await r.text())); } else { await this.create(); } },
  closeForm(){ this.showForm=false; this.editingId=null; this.form={code:'',name:'',start_time:'07:00',end_time:'15:00',duration_hours:8.0,day_group_id:''}; },
  async del(id){ if(!confirm('删除?')) return; const r=await fetch('/api/v1/shift-templates/'+id,{method:'DELETE',headers:{Authorization:tok()}}); if(!r.ok){const t=await r.text(); alert(t);} await this.load(); },
  toggleSort(key){
    if (this.sortBy === key) {
      this.sortDir = this.sortDir === 'asc' ? 'desc' : 'asc';
    } else {
      this.sortBy = key;
      this.sortDir = 'asc';
    }
  },
}}
function dg(){ return {
  isSuper: localStorage.getItem('user_role') === 'super_admin',
  get canManage(){ return canManageScheduling(); },
  items:[], loading:true, show:false, editingId:null, sortBy:'name', sortDir:'asc',
  total:0, page:0, pageSize:50, loadingMore:false,
  tenantId: new URLSearchParams(location.search).get('tenant_id') || '',
  form:{name:'',description:'',day_numbers_str:''},
  get canCreateTarget(){ return !this.isSuper || Boolean(this.tenantId); },
  tenantQuery(){ return this.tenantId ? '?tenant_id='+encodeURIComponent(this.tenantId) : ''; },
  get sortedDgItems(){
    const key = this.sortBy, dir = this.sortDir === 'asc' ? 1 : -1;
    const cmp = (a, b) => {
      let av, bv;
      if (key === 'days') { av = (a.day_numbers||[]).join(','); bv = (b.day_numbers||[]).join(','); }
      else { av = a[key] ?? ''; bv = b[key] ?? ''; }
      if (av < bv) return -1;
      if (av > bv) return 1;
      return 0;
    };
    return this.items.slice().sort((x, y) => dir * cmp(x, y));
  },
  get hasMore(){ return this.items.length < this.total; },
  async load(){
    this.loading=true;
    try { await this.loadGroups(true); } finally { this.loading=false; }
  },
  async loadGroups(reset=false){
    const page = reset ? 1 : this.page + 1;
    if (!reset) this.loadingMore=true;
    try {
      const query=this.tenantQuery() ? this.tenantQuery()+'&' : '?';
      const r=await fetch(`/api/v1/day-groups${query}page=${page}&page_size=${this.pageSize}`,{headers:{Authorization:tok()}}); if(r.ok) {
        const data=await r.json();
        this.items=reset ? data.items : this.items.concat(data.items);
        this.total=data.total ?? this.items.length;
        this.page=data.page ?? page;
      }
    } finally { if(!reset) this.loadingMore=false; }
  },
  async loadMore(){ if(!this.loadingMore && this.hasMore) await this.loadGroups(false); },
  async edit(d){
    this.editingId=d.id;
    this.form={name:d.name,description:d.description||'',day_numbers_str:(d.day_numbers||[]).join(',')};
    this.show=true;
  },
  async submit(){
    const days=this.form.day_numbers_str.split(',').map(s=>parseInt(s.trim())).filter(n=>n>=1&&n<=7);
    const body=JSON.stringify({name:this.form.name,description:this.form.description||null,day_numbers:days});
    const url=this.editingId ? `/api/v1/day-groups/${this.editingId}` : `/api/v1/day-groups${this.tenantQuery()}`;
    const r=await fetch(url,{method:this.editingId ? 'PATCH' : 'POST',headers:{'Content-Type':'application/json',Authorization:tok()},body});
    if(r.ok){this.closeForm(); await this.load();} else alert((await r.text()));
  },
  closeForm(){ this.show=false; this.editingId=null; this.form={name:'',description:'',day_numbers_str:''}; },
  async del(id){ if(!confirm('删除?')) return; const r=await fetch('/api/v1/day-groups/'+id,{method:'DELETE',headers:{Authorization:tok()}}); if(!r.ok){const t=await r.text(); alert(t);} await this.load(); },
  toggleSort(key){
    if (this.sortBy === key) {
      this.sortDir = this.sortDir === 'asc' ? 'desc' : 'asc';
    } else {
      this.sortBy = key;
      this.sortDir = 'asc';
    }
  },
}}
