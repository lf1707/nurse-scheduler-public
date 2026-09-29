function tok(){ return ''; }
function tenantsPage(){ return {
  rows: [], loading: true, error: '', notice: '', loadSequence: 0,
  planLimits: null,
  get systemPlanKeys(){
    return ['demo', 'free', 'pro', 'max'].filter(plan => !!this.planLimits?.[plan]);
  },
  get customPlanKeys(){
    return (this.planLimits?.custom || []).map(plan => plan.key);
  },
  get createPlanOptions(){
    return [...this.systemPlanKeys, ...this.customPlanKeys];
  },
  get editPlanOptions(){
    return [...this.systemPlanKeys.filter(plan => plan !== 'demo'), ...this.customPlanKeys];
  },
  showCreate: false,
  showEdit: false, editLoading: false, editingRow: null,
  editForm: {tenantId:'', slug:'', name:'', admin_email:'', admin_user_id:'', admin_mfa_enabled:false, is_active:true, plan:'free', subscription_type:'manual', seat_packs:0, starts_at:'', ends_at:'', subscribed:false},
  createForm: {
    name:'', slug:'',
    admin_email:'', admin_password:'',
    admin_first_name:'', admin_last_name:'',
    plan:'free', subscription_type:'manual', starts_at:new Date().toISOString().slice(0,10), ends_at:'', subscribed:false,
  },
  async load(){
    const sequence = ++this.loadSequence;
    this.loading=true; this.error=''; this.notice='';
    try {
      const [tr, sr, lr] = await Promise.all([
        fetch('/api/v1/tenants?page_size=100', {cache:'no-store', headers:{Authorization:tok()}}),
        fetch('/api/v1/subscriptions', {cache:'no-store', headers:{Authorization:tok()}}),
        fetch('/api/v1/admin/plan-limits', {cache:'no-store', headers:{Authorization:tok()}}),
      ]);
      if (!tr.ok || !sr.ok) throw new Error(await this.errorMessage(tr.ok ? sr : tr, '加载失败(需要超级管理员)'));
      const tenants=(await tr.json()).items;
      const subs=await sr.json();
      if(sequence !== this.loadSequence) return;
      if (lr.ok) this.planLimits = await lr.json();
      if (!this.createPlanOptions.includes(this.createForm.plan)) {
        this.createForm.plan = this.editPlanOptions[0] || '';
      }
      this.rows=tenants.map(t => {
        const s=subs.find(x => x.tenant_id===t.id) || null;
        return {tenant:t, sub:s, editing:false, edit:{
          name:t.name,
          plan:s ? s.plan : 'free',
          subscription_type:s ? s.subscription_type : 'manual',
          seat_packs:s ? s.seat_packs : 0,
          starts_at:s ? s.starts_at.slice(0,10) : new Date().toISOString().slice(0,10),
          ends_at:s && s.ends_at ? s.ends_at.slice(0,10) : '',
          subscribed:s ? Boolean(!s.is_canceled) : true,
        }};
      });
    } catch(ex) {
      if(sequence !== this.loadSequence) return;
      this.error=ex.message;
    }
    this.loading=false;
    if(new URLSearchParams(window.location.search).get('saved')){
      this.notice='租户已保存';
      window.history.replaceState(null, '', '/pages/tenants');
    }
    if(new URLSearchParams(window.location.search).get('created')){
      this.notice='租户与管理员已创建，可使用管理员邮箱登录';
      window.history.replaceState(null, '', '/pages/tenants');
    }
  },
  planLabel(plan){
    if (!this.planLimits) return plan;
    const custom=(this.planLimits.custom || []).find(item => item.key === plan);
    const max_nurses=custom ? custom.max_nurses : this.planLimits[plan]?.max_nurses;
    const max_period_days=custom ? custom.max_period_days : this.planLimits[plan]?.max_period_days;
    const nurses=max_nurses===null || max_nurses===undefined ? '不限' : `最多${max_nurses}护士`;
    const days=max_period_days===null || max_period_days===undefined ? '排班不限' : `排班${max_period_days}天`;
    const fixed=plan==='demo'?'·固定':'';
    return `${plan}（${nurses}·${days}${fixed}）`;
  },
  seatPackEnabled(plan){
    if (plan === 'demo' || !this.planLimits) return false;
    const custom = (this.planLimits.custom || []).find(item => item.key === plan);
    const definition = this.planLimits[plan] || custom;
    return Boolean(definition && definition.max_nurses !== null);
  },
  async create(){
    this.error=''; this.notice='';
    const payload={
      name:this.createForm.name,
      slug:this.createForm.slug,
      admin:{
        email:this.createForm.admin_email.trim().toLowerCase(),
        password:this.createForm.admin_password,
        first_name:this.createForm.admin_first_name.trim(),
        last_name:this.createForm.admin_last_name.trim(),
      },
    };
    if(this.createForm.subscribed){
      payload.subscription={
        plan:this.createForm.plan,
        subscription_type:'manual',
        starts_at:this.createForm.starts_at ? new Date(this.createForm.starts_at+'T00:00:00Z').toISOString() : null,
        ends_at:this.createForm.ends_at ? new Date(this.createForm.ends_at+'T23:59:59Z').toISOString() : null,
        is_canceled:false,
      };
    }
    const r=await fetch('/api/v1/tenants/with-admin', {
      method:'POST',
      headers:{'Content-Type':'application/json', Authorization:tok()},
      body:JSON.stringify(payload),
    });
    if(!r.ok){ this.error='租户创建失败: '+await this.errorMessage(r); return; }
    this.showCreate=false;
    this.resetCreateForm();
    window.location.href='/pages/tenants?created=1';
  },
  resetCreateForm(){
    this.createForm={
      name:'', slug:'', admin_email:'', admin_password:'',
      admin_first_name:'', admin_last_name:'',
      plan:'free', starts_at:new Date().toISOString().slice(0,10), ends_at:'', subscribed:false,
      subscription_type:'manual',
    };
  },
  subscriptionStatus(row){
    if(!row.sub) return {text:'未开通', cls:'tag-fail'};
    if(row.sub.is_canceled) return {text:'已取消', cls:'tag-fail'};
    if(row.sub.ends_at && new Date(row.sub.ends_at) <= new Date()) return {text:'已到期', cls:'tag-fail'};
    return {text:'生效中', cls:'tag-ok'};
  },
  async openEdit(row){
    this.editingRow=row; this.showEdit=true; this.editLoading=true;
    this.error=''; this.notice='';
    this.editForm={
      tenantId:row.tenant.id, slug:row.tenant.slug, name:row.tenant.name,
      admin_email:'', admin_user_id:'', admin_mfa_enabled:false, admin_missing:false,
      admin_password:'', admin_first_name:'', admin_last_name:'',
      is_active:row.tenant.is_active, plan:row.edit.plan,
      subscription_type:row.edit.subscription_type,
      seat_packs:row.edit.seat_packs,
      starts_at:row.edit.starts_at, ends_at:row.edit.ends_at,
      subscribed:row.edit.subscribed,
    };
    const r=await fetch('/api/v1/tenants/'+encodeURIComponent(row.tenant.id)+'/admin', {headers:{Authorization:tok()}});
    if(r.status===404){
      this.editForm.admin_missing=true;
      this.editLoading=false;
      return;
    }
    if(!r.ok){
      this.error='管理员信息加载失败: '+await this.errorMessage(r);
      this.editLoading=false; return;
    }
    const admin=await r.json();
    this.editForm.admin_email=admin.email;
    this.editForm.admin_user_id=admin.id;
    this.editForm.admin_mfa_enabled=Boolean(admin.mfa_enabled);
    this.editLoading=false;
  },
  closeEdit(){
    this.showEdit=false; this.editLoading=false; this.editingRow=null;
  },
  async resetAdminMfa(){
    if(!this.editForm.admin_user_id || !this.editForm.admin_mfa_enabled) return;
    if(!confirm('确认重置该租户管理员 '+this.editForm.admin_email+' 的两步验证？管理员将失去当前恢复码，需重新开启 MFA，且现有会话会被吊销。')) return;
    const r=await fetch('/api/v1/admin/users/'+encodeURIComponent(this.editForm.admin_user_id)+'/mfa/reset', {
      method:'POST', headers:{Authorization:tok()},
    });
    if(!r.ok){ this.error='重置失败: '+await this.errorMessage(r); return; }
    this.notice='该租户管理员的两步验证已重置，其会话已吊销，需重新开启 MFA';
  },
  async saveEdit(){
    if(!this.editingRow || this.editLoading) return;
    this.error=''; this.notice='';
    if(this.editForm.admin_missing){
      if(!this.editForm.admin_email || !this.editForm.admin_password || !this.editForm.admin_first_name || !this.editForm.admin_last_name){
        this.error='请填写管理员邮箱、密码、姓和名';
        return;
      }
      const adminBody={
        email:this.editForm.admin_email.trim().toLowerCase(),
        password:this.editForm.admin_password,
        first_name:this.editForm.admin_first_name.trim(),
        last_name:this.editForm.admin_last_name.trim(),
      };
      const adminR=await fetch('/api/v1/tenants/'+this.editForm.tenantId+'/admin', {
        method:'POST', headers:{'Content-Type':'application/json', Authorization:tok()},
        body:JSON.stringify(adminBody),
      });
      if(!adminR.ok){ this.error='管理员创建失败: '+await this.errorMessage(adminR); return; }
      this.editForm.admin_missing=false;
      const created=await adminR.json();
      this.editForm.admin_user_id=created.id;
      this.editForm.admin_mfa_enabled=false;
    }
    const tenantBody={
      name:this.editForm.name.trim(), is_active:this.editForm.is_active,
      admin_email:this.editForm.admin_email,
    };
    const tenantR=await fetch('/api/v1/tenants/'+this.editForm.tenantId, {
      method:'PATCH', headers:{'Content-Type':'application/json', Authorization:tok()},
      body:JSON.stringify(tenantBody),
    });
    if(!tenantR.ok){ this.error='租户保存失败: '+await this.errorMessage(tenantR); return; }
    const subBody={
      plan:this.editForm.plan,
      subscription_type:this.editForm.subscription_type,
      seat_packs:Number(this.editForm.seat_packs || 0),
      starts_at:this.editForm.starts_at ? new Date(this.editForm.starts_at+'T00:00:00Z').toISOString() : null,
      ends_at:this.editForm.ends_at ? new Date(this.editForm.ends_at+'T23:59:59Z').toISOString() : null,
      is_canceled:!this.editForm.subscribed,
    };
    const subR=await fetch('/api/v1/subscriptions/'+this.editForm.tenantId, {
      method:'PUT', headers:{'Content-Type':'application/json', Authorization:tok()}, body:JSON.stringify(subBody),
    });
    if(!subR.ok){
      this.error='订阅保存失败: '+await this.errorMessage(subR);
      this.closeEdit(); await this.load(); return;
    }
    this.closeEdit();
    window.location.href='/pages/tenants?saved=1';
  },
	  async remove(row){
    this.error=''; this.notice='';
    const confirmed=confirm(`确定删除租户「${row.tenant.name}」吗？\n\n将永久删除该租户的全部用户、护士、班次、规则、订阅和排班记录，且无法恢复。`);
    if(!confirmed) return;
    const r=await fetch('/api/v1/tenants/'+row.tenant.id, {method:'DELETE', headers:{Authorization:tok()}});
    if(!r.ok){ this.error='删除失败: '+await this.errorMessage(r); return; }
    this.notice='租户已删除';
    await this.load();
  },
  async errorMessage(response, fallback='请求失败'){
    const text=await response.text().catch(() => '');
    try { const body=JSON.parse(text); return body.detail || fallback; } catch(_) { return text || fallback; }
  },
}}
