function tok(){ return ''; }
function usersPage(){ return {
  users: [], nurses: [], tenants: [], nurseNames: {}, currentUserId: '', loading: true,
  error: '', notice: '', showCreate: false, showEdit: false,
  isSuper: localStorage.getItem('user_role') === 'super_admin',
  role: localStorage.getItem('user_role') || '',
  tenantId: new URLSearchParams(location.search).get('tenant_id') || '',
  currentTenantName: '', currentTenantId: '', currentTenantSlug: '',
  createForm: {email:'', password:'', first_name:'', last_name:'', role:'viewer', nurse_id:''},
  editForm: {id:'', email:'', password:'', first_name:'', last_name:'', role:'viewer', nurse_id:'', is_active:true},
  roleNames: {
    tenant_admin:'租户管理员', scheduler:'排班员', nurse:'护士',
    viewer:'访客', super_admin:'超级管理员',
  },
  emptyCreateForm(){ return {email:'', password:'', first_name:'', last_name:'', role:'viewer', nurse_id:''}; },
  emptyEditForm(){ return {id:'', tenant_id:'', email:'', password:'', first_name:'', last_name:'', role:'viewer', nurse_id:'', is_active:true}; },
  tenantQuery(){ return this.tenantId ? '?tenant_id='+encodeURIComponent(this.tenantId) : ''; },
  get canCreate(){
    const form=this.createForm;
    return Boolean(form.email && form.password.length>=8 && form.first_name && form.last_name &&
      (form.role!=='nurse' || form.nurse_id));
  },
  get canUpdate(){
    const form=this.editForm;
    return Boolean(form.id && form.email && form.first_name && form.last_name &&
      (form.role!=='nurse' || form.nurse_id) && (!form.password || form.password.length>=8));
  },
  async load(){
    this.loading=true; this.error=''; this.notice='';
    const me=await fetch('/api/v1/auth/me', {headers:{Authorization:tok()}});
    if(!me.ok){
      this.error=await this.errorMessage(me, '登录状态加载失败');
      this.users=[]; this.loading=false;
      return;
    }
    const profile=await me.json();
    this.role=profile.role;
    this.isSuper=profile.role==='super_admin';
    this.currentUserId=profile.id;
    this.currentTenantId=this.tenantId || profile.tenant_id || '';
    this.currentTenantName=profile.tenant_name || '';
    this.currentTenantSlug=profile.tenant_slug || '';
    localStorage.setItem('user_role', profile.role);
    if (this.isSuper && !this.tenantId) {
      await this.loadTenants();
      this.users=[]; this.loading=false;
      return;
    }
    const [r,n,t]=await Promise.all([
      fetch('/api/v1/auth/users'+this.tenantQuery(), {headers:{Authorization:tok()}}),
      fetch('/api/v1/nurses?page_size=100'+(this.tenantId ? '&tenant_id='+encodeURIComponent(this.tenantId) : ''), {headers:{Authorization:tok()}}),
      this.isSuper && this.tenantId ? fetch('/api/v1/tenants/'+encodeURIComponent(this.tenantId), {headers:{Authorization:tok()}}) : Promise.resolve(null),
    ]);
    if(!r.ok){ this.error=await this.errorMessage(r, '加载失败(需要租户管理员)'); this.users=[]; this.loading=false; return; }
    const users=await r.json();
    this.users=this.isSuper ? users : users.filter(user => user.role !== 'tenant_admin');
    this.nurses=n.ok ? (await n.json()).items : [];
    const tenant=t && t.ok ? await t.json() : null;
    if(tenant){
      this.currentTenantName=tenant.name;
      this.currentTenantSlug=tenant.slug;
    }
    this.nurseNames=Object.fromEntries(this.nurses.map(nurse => [
      nurse.id, nurse.last_name + nurse.first_name + '（' + nurse.employee_id + '）',
    ]));
    this.loading=false;
  },
  async loadTenants(){
    try {
      const r=await fetch('/api/v1/tenants?page_size=100', {cache:'no-store', headers:{Authorization:tok()}});
      if(!r.ok) throw new Error(await this.errorMessage(r, '加载租户失败'));
      this.tenants=await r.json().then(body=>body.items);
    } catch (error) { this.error=error.message; }
  },
  selectTenant(tenantId){
    const url=new URL('/pages/users', window.location.origin);
    if(tenantId) url.searchParams.set('tenant_id', tenantId);
    window.location.href=url.href;
  },
  nurseOptions(currentNurseId){
    const used=new Set(this.users.map(user => user.nurse_id).filter(Boolean));
    const keep=currentNurseId || this.editForm.nurse_id || '';
    const options=this.nurses.filter(nurse => !used.has(nurse.id) || nurse.id===keep);
    const linked=this.nurses.find(nurse => nurse.id===keep);
    return linked && !options.some(nurse => nurse.id===linked.id) ? [linked, ...options] : options;
  },
  toggleCreate(){
    this.showCreate=!this.showCreate;
    if(!this.showCreate) this.createForm=this.emptyCreateForm();
  },
  async create(){
    this.error=''; this.notice='';
    if(!this.canCreate) return;
    const payload={...this.createForm};
    if(payload.role!=='nurse') payload.nurse_id=null;
    const r=await fetch('/api/v1/auth/users'+this.tenantQuery(), {
      method:'POST', headers:{'Content-Type':'application/json', Authorization:tok()},
      body:JSON.stringify(payload),
    });
    if(!r.ok){ this.error='创建失败: '+await this.errorMessage(r); return; }
    this.showCreate=false; this.createForm=this.emptyCreateForm();
    this.notice='用户已创建，可使用邮箱和初始密码登录';
    await this.load();
  },
  editUser(user){
    this.showCreate=false;
    this.showEdit=true; this.error=''; this.notice='';
    this.editForm={
      id:user.id, tenant_id:user.tenant_id || this.tenantId || this.currentTenantId || '', email:user.email, password:'', first_name:user.first_name,
      last_name:user.last_name, role:user.role, nurse_id:user.nurse_id || '',
      is_active:user.is_active,
    };
  },
  closeEdit(){ this.showEdit=false; this.editForm=this.emptyEditForm(); },
  async resetMfa(user){
    this.error=''; this.notice='';
    if(!confirm('确认重置 '+user.email+' 的两步验证？该用户将失去当前恢复码，需重新开启 MFA。')) return;
    const r=await fetch('/api/v1/admin/users/'+encodeURIComponent(user.id)+'/mfa/reset', {
      method:'POST', headers:{Authorization:tok()},
    });
    if(!r.ok){ this.error='重置失败: '+await this.errorMessage(r); return; }
    this.notice=user.email+' 的两步验证已重置，已吊销其会话，需重新开启 MFA';
    await this.load();
  },
  async remove(user){
    this.error=''; this.notice='';
    if(!confirm('确认删除 '+user.email+'？该用户将无法登录，其排班审批记录将保留。')) return;
    const targetTenantId=this.tenantId || this.currentTenantId;
    const targetQuery=targetTenantId ? '?tenant_id='+encodeURIComponent(targetTenantId) : '';
    const r=await fetch('/api/v1/auth/users/'+encodeURIComponent(user.id)+targetQuery, {
      method:'DELETE', headers:{Authorization:tok()},
    });
    if(!r.ok){ this.error='删除失败: '+await this.errorMessage(r); return; }
    this.notice=user.email+' 已删除';
    await this.load();
  },
  async update(){
    this.error=''; this.notice='';
    if(!this.canUpdate) return;
    const {id, email, ...payload}=this.editForm;
    if(!payload.password) delete payload.password;
    delete payload.role;  // 系统角色创建后不可修改
    if(this.editForm.role!=='nurse') payload.nurse_id=null;
    if(id===this.currentUserId) delete payload.is_active;
    const targetTenantId=this.tenantId || this.editForm.tenant_id || this.currentTenantId;
    if(this.isSuper && !targetTenantId){
      this.error='请先选择租户后再保存';
      return;
    }
    const targetQuery=targetTenantId ? '?tenant_id='+encodeURIComponent(targetTenantId) : '';
    const r=await fetch('/api/v1/auth/users/'+id+targetQuery, {
      method:'PATCH', headers:{'Content-Type':'application/json', Authorization:tok()},
      body:JSON.stringify(payload),
    });
    if(!r.ok){ this.error='保存失败: '+await this.errorMessage(r); return; }
    this.closeEdit(); this.notice='用户已更新';
    await this.load();
  },
  syncNurse(){
    const nurse=this.nurses.find(item => item.id===this.createForm.nurse_id);
    if(!nurse) return;
    this.createForm.first_name=nurse.first_name;
    this.createForm.last_name=nurse.last_name;
  },
  syncEditNurse(){
    const nurse=this.nurses.find(item => item.id===this.editForm.nurse_id);
    if(!nurse) return;
    this.editForm.first_name=nurse.first_name;
    this.editForm.last_name=nurse.last_name;
  },
  async errorMessage(response, fallback='请求失败'){
    const text=await response.text().catch(() => '');
    try {
      const body=JSON.parse(text);
      const detail=body?.detail;
      if(typeof detail === 'string') return detail;
      if(detail?.reason === 'mfa_enrollment_required') return '需要先开启两步验证';
      if(detail?.reason) return detail.reason;
      if(detail?.message) return detail.message;
      if(detail) return JSON.stringify(detail);
      return body?.message || fallback;
    } catch(_) { return text || fallback; }
  },
}}
