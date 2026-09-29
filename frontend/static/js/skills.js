function tok(){ return ''; }
function skillsPage(){
  return {
    isSuper: localStorage.getItem('user_role') === 'super_admin',
    items: [], tenants: [], loading: true, showForm: false, editingSkill: false,
    tenantId: new URLSearchParams(location.search).get('tenant_id') || '',
    get canManage(){ return this.isSuper; },
    get canCreateTarget(){ return !this.isSuper || Boolean(this.tenantId); },
    get tenantQuery(){ return this.tenantId ? '?tenant_id='+encodeURIComponent(this.tenantId) : ''; },
    reloadForTenant(){ location.search = this.tenantId ? '?tenant_id='+encodeURIComponent(this.tenantId) : ''; },
    form: {id:'', code:'', name:''},
    async load(){
      this.loading=true;
      const [r, tenants]=await Promise.all([
        fetch('/api/v1/skills'+(this.tenantQuery ? this.tenantQuery+'&' : '?')+'page_size=100',{headers:{Authorization:tok()},cache:'no-store'}),
        this.isSuper
          ? fetch('/api/v1/tenants?page_size=100',{headers:{Authorization:tok()},cache:'no-store'})
          : Promise.resolve(null),
      ]);
      if(r.ok) this.items=(await r.json()).items;
      if(tenants && tenants.ok) this.tenants=(await tenants.json()).items;
      this.loading=false;
    },
    startEdit(skill){
      this.editingSkill = true;
      this.form = {id: skill.id, code: skill.code, name: skill.name};
      this.showForm = true;
    },
    cancelEdit(){
      this.showForm = false;
      this.editingSkill = false;
      this.form = {id:'', code:'', name:''};
    },
    async saveSkill(){
      const isEdit = this.editingSkill;
      const url = isEdit ? '/api/v1/skills/'+this.form.id : '/api/v1/skills';
      const method = isEdit ? 'PATCH' : 'POST';
      const {id, ...body} = this.form;
      const query = this.isSuper && !isEdit ? this.tenantQuery : '';
      const r=await fetch(url+query,{method:method,headers:{'Content-Type':'application/json',Authorization:tok()},body:JSON.stringify(body)});
      if(r.ok){
        this.cancelEdit();
        await this.load();
      } else alert((isEdit?'更新':'创建')+'失败: '+(await r.text()));
    },
    async del(id){
      if(!confirm('删除该技能?')) return;
      const r=await fetch('/api/v1/skills/'+id,{method:'DELETE',headers:{Authorization:tok()}});
      if(!r.ok){ alert('删除失败: '+(await r.text())); }
      await this.load();
    },
  }
}
