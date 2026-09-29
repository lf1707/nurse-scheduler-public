function tok(){ return ''; }
const NO_DEPARTMENT = '__none__';
function nursesPage(){
  return {
    userRole: localStorage.getItem('user_role') || '',
    isSuper: localStorage.getItem('user_role') === 'super_admin',
    get canManage(){ return this.userRole === 'super_admin' || this.userRole === 'tenant_admin'; },
    items: [], roles: [], skills: [], tenants: [], loading: true, showForm: false, deptFilter: '',
    noDepartment: NO_DEPARTMENT,
    allDepartments: [],
    nurseTotal: 0, nursePage: 0, nursePageSize: 50, nurseLoadingMore: false,
    sub: null,
    planLimits: null,
    tenantSlug: '',
    tenantId: new URLSearchParams(location.search).get('tenant_id') || '',
    get canCreateTarget(){ return !this.isSuper || Boolean(this.tenantId); },
    get canCreate(){
      const f=this.form;
      return Boolean(f.employee_id && f.first_name && f.last_name &&
        f.role_ids.length > 0);
    },
    tenantQuery(){ return this.tenantId ? '?tenant_id='+encodeURIComponent(this.tenantId) : ''; },
    tenantParam(){ return this.tenantId ? 'tenant_id='+encodeURIComponent(this.tenantId) : ''; },
    reloadForTenant(){ location.search=this.tenantId ? '?tenant_id='+encodeURIComponent(this.tenantId) : ''; },
    get tenantName(){ return this.tenants.find(tenant => tenant.id === this.tenantId)?.name || ''; },
    get isDemo(){ return this.tenantSlug === 'demo'; },
    get planLimitText(){
      if (this.planLimits?.max_nurses === null) return '不限';
      return this.planLimits?.max_nurses ?? '-';
    },
    roleLabel(id){
      const role = this.roles.find(role => role.id === id);
      return `${role?.code ?? id} - ${role?.name ?? ''}`;
    },
    get availableRoles(){ return this.tenantId ? this.roles.filter(role => role.tenant_id === this.tenantId) : this.roles; },
    get availableSkills(){ return this.tenantId ? this.skills.filter(skill => skill.tenant_id === this.tenantId) : this.skills; },
    skillLabel(id){
      const skill = this.skills.find(skill => skill.id === id);
      return `${skill?.code ?? id} - ${skill?.name ?? ''}`;
    },
    roleCodes(nurse){
      return (nurse.role_ids || []).map(id => this.roles.find(role => role.id === id)?.code ?? id).join(', ');
    },
    skillCodes(nurse){
      return (nurse.skill_ids || []).map(id => this.skills.find(skill => skill.id === id)?.code ?? id).join(', ');
    },
    sortBy: 'employee_id', sortDir: 'asc',
    get departments(){ return [...new Set(this.items.map(n=>n.department).filter(Boolean))].sort(); },
    get filtered(){
      let list = this.deptFilter === NO_DEPARTMENT
        ? this.items.filter(n=>!n.department)
        : this.deptFilter ? this.items.filter(n=>n.department===this.deptFilter) : this.items;
      const key = this.sortBy, dir = this.sortDir === 'asc' ? 1 : -1;
      const cmp = (a, b) => {
        let av, bv;
        if (key === 'name') { av = a.last_name + a.first_name; bv = b.last_name + b.first_name; }
        else if (key === 'roles') { av = (a.role_ids||[]).map(id => (this.roles.find(r=>r.id===id)||{code:id}).code).join(', '); bv = (b.role_ids||[]).map(id => (this.roles.find(r=>r.id===id)||{code:id}).code).join(', '); }
        else if (key === 'skills') { av = (a.skill_ids||[]).map(id => (this.skills.find(s=>s.id===id)||{code:id}).code).join(', '); bv = (b.skill_ids||[]).map(id => (this.skills.find(s=>s.id===id)||{code:id}).code).join(', '); }
        else { av = a[key] ?? ''; bv = b[key] ?? ''; }
        if (av < bv) return -1;
        if (av > bv) return 1;
        return 0;
      };
      return list.slice().sort((x, y) => dir * cmp(x, y));
    },
    _defaultContract: {shifts_per_period:10, max_shifts_per_period:null, min_rest_hours:11, max_consecutive_days:5, enforce_balanced:true, enforce_shifts_per_period:true, enforce_one_shift_per_day:true},
    deptToDelete: '',
    form: {employee_id:'', first_name:'', last_name:'', department:'', is_available:true, role_ids:[], skill_ids:[], contract:null},
    async load(){
      try {
        const u = await fetchMeCached(tok());
        this.tenantSlug = u.tenant_slug || '';
      } catch (_) {}
      if (this.isSuper) {
        const r = await fetch('/api/v1/tenants?page_size=100', {headers:{Authorization:tok()}});
        if (r.ok) this.tenants = (await r.json()).items;
      }
      if (!this.isSuper) {
        try {
          const r = await fetch('/api/v1/subscriptions/me', {headers:{Authorization:tok()}});
          if (r.ok) this.sub = await r.json();
        } catch (_) {}
        try {
          const r = await fetch('/api/v1/subscriptions/me/limits', {cache:'no-store', headers:{Authorization:tok()}});
          if (r.ok) this.planLimits = await r.json();
        } catch (_) {}
      }
      this.loading = true;
      try {
        await Promise.all([
          this.loadNurses(true),
          this.loadRoles(),
          this.loadSkills(),
          this.loadDepartments(),
        ]);
      } finally {
        this.loading = false;
      }
    },
    async loadNurses(reset=false){
      const page = reset ? 1 : this.nursePage + 1;
      if (!reset) this.nurseLoadingMore = true;
      try {
        let url = `/api/v1/nurses?page=${page}&page_size=${this.nursePageSize}`;
        if (this.tenantId) url += '&tenant_id=' + encodeURIComponent(this.tenantId);
        if (this.deptFilter === NO_DEPARTMENT) url += '&department_missing=true';
        else if (this.deptFilter) url += '&department=' + encodeURIComponent(this.deptFilter);
        const r = await fetch(url, {headers:{Authorization:tok()}});
        if (!r.ok) return;
        const data = await r.json();
        this.items = reset ? data.items : this.items.concat(data.items);
        this.nurseTotal = data.total ?? this.items.length;
        this.nursePage = data.page ?? page;
      } finally {
        if (!reset) this.nurseLoadingMore = false;
      }
    },
    async loadDepartments(){
      const r = await fetch(`/api/v1/nurses/departments${this.tenantQuery()}`, {headers:{Authorization:tok()}});
      if (r.ok) this.allDepartments = await r.json();
    },
    get hasMoreNurses(){
      return this.items.length < this.nurseTotal;
    },
    async loadMoreNurses(){
      if (this.nurseLoadingMore || !this.hasMoreNurses) return;
      await this.loadNurses(false);
    },
    async loadRoles(reset=false){ if(!reset && this.roles.length) return; const separator=this.tenantId ? '&' : '?'; const r=await fetch(`/api/v1/roles${this.tenantQuery()}${separator}page_size=100`,{headers:{Authorization:tok()}}); if(r.ok) this.roles=(await r.json()).items; },
    toggleSort(key){
      if (this.sortBy === key) {
        this.sortDir = this.sortDir === 'asc' ? 'desc' : 'asc';
      } else {
        this.sortBy = key;
        this.sortDir = 'asc';
      }
    },
    async loadSkills(reset=false){ if(!reset && this.skills.length) return; const r=await fetch(`/api/v1/skills${this.tenantQuery()}${this.tenantId ? '&' : '?'}page_size=100`,{headers:{Authorization:tok()}}); if(r.ok) this.skills=(await r.json()).items; },
    toggleContract(enabled){
      if(enabled){
        this.form.contract = {...this._defaultContract};
      } else {
        this.form.contract = null;
      }
    },
    async removeDepartment(department){
      const count=this.items.filter(n => n.department === department).length;
      if(!confirm(`删除科室/部门“${department}”？将清空 ${count} 名护士的科室/部门。`)) return;
      let departmentUrl='/api/v1/nurses/departments?department='+encodeURIComponent(department);
      if(this.tenantId) departmentUrl += '&tenant_id='+encodeURIComponent(this.tenantId);
      const r=await fetch(departmentUrl,{method:'DELETE',headers:{Authorization:tok()}});
      if(r.ok){
        if(this.form.department === department) this.form.department='';
        if(this.deptFilter === department) this.deptFilter='';
        await this.loadDepartments();
        await this.load();
      } else alert('删除科室/部门失败: '+(await r.text()));
    },
    async createNurse(){
      const r=await fetch('/api/v1/nurses',{method:'POST',headers:{'Content-Type':'application/json',Authorization:tok()},body:JSON.stringify(this.form)});
      if(r.ok){this.showForm=false; this.form={employee_id:'',first_name:'',last_name:'',department:'',is_available:true,role_ids:[],skill_ids:[]}; await this.load();}
      else alert('创建失败: '+(await r.text()));
    },
    async editNurse(nurse){
      // 预填表单
      this.form = {
        employee_id: nurse.employee_id,
        first_name: nurse.first_name,
        last_name: nurse.last_name,
        department: nurse.department || '',
        is_available: nurse.is_available,
        role_ids: nurse.role_ids || [],
        skill_ids: nurse.skill_ids || [],
        contract: nurse.contract ? {
          shifts_per_period: nurse.contract.shifts_per_period,
          max_shifts_per_period: nurse.contract.max_shifts_per_period,
          min_rest_hours: nurse.contract.min_rest_hours,
          max_consecutive_days: nurse.contract.max_consecutive_days,
          enforce_balanced: nurse.contract.enforce_balanced,
          enforce_shifts_per_period: nurse.contract.enforce_shifts_per_period,
          enforce_one_shift_per_day: nurse.contract.enforce_one_shift_per_day
        } : null,
        id: nurse.id
      };
      this.showForm = true;
      await this.loadRoles();
      await this.loadSkills();
    },
    async createOrUpdateNurse(){
      const isEdit = !!this.form.id;
      const url = isEdit ? '/api/v1/nurses/'+this.form.id : '/api/v1/nurses';
      const method = isEdit ? 'PATCH' : 'POST';
      const {id, ...body} = this.form;  // 不发送 id
      // Sanitize contract numeric fields: non-finite → null
      if (body.contract) {
        const msp = body.contract.max_shifts_per_period;
        body.contract.max_shifts_per_period = (typeof msp === 'number' && !isNaN(msp) && isFinite(msp)) ? msp : null;
      } else {
        body.contract = null;
      }
      const requestUrl = isEdit ? url : url + this.tenantQuery();
      const r=await fetch(requestUrl,{method:method,headers:{'Content-Type':'application/json',Authorization:tok()},body:JSON.stringify(body)});
      if(r.ok){
        this.cancelNurseEdit();
        await this.load();
      } else {
        let msg = await r.text();
        try { msg = JSON.parse(msg).detail || msg; } catch(_) {}
        alert((isEdit?'更新':'创建')+'失败: '+msg);
      }
    },
    cancelNurseEdit(){
      this.showForm = false;
      this.form = {employee_id:'',first_name:'',last_name:'',department:'',is_available:true,role_ids:[],skill_ids:[],contract:null};
    },
    async del(id){
      if(!confirm('删除该护士?')) return;
      await fetch('/api/v1/nurses/'+id,{method:'DELETE',headers:{Authorization:tok()}});
      await this.load();
    },
  }
}
