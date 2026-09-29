function tok(){ return ''; }
function lst(){ return {
  isSuper: localStorage.getItem('user_role') === 'super_admin',
  role: localStorage.getItem('user_role') || '',
  currentUserId:'',
  canDelete: ['super_admin','tenant_admin','scheduler'].includes(localStorage.getItem('user_role')||''),
  canActivate: ['super_admin','tenant_admin','scheduler'].includes(localStorage.getItem('user_role')||''),
  items:[], loading:true,
  sortBy:'display_id', sortDir:'desc',
  async init(){
    try {
      const u = await fetchMeCached(tok());
      this.role = u.role;
      this.currentUserId = u.id;
      this.isSuper = u.role === 'super_admin';
      this.canDelete = ['super_admin','tenant_admin','scheduler'].includes(u.role);
    } catch (_) {}
    await this.load();
  },
  canCancel(item){
    if (item.status !== 'pending' && item.status !== 'running') return false;
    if (this.isSuper || this.role === 'tenant_admin') return true;
    return this.role === 'scheduler' && item.requested_by === this.currentUserId;
  },
  async load(){
    this.loading=true;
    const params=new URLSearchParams({page_size:'100',sort_by:this.sortBy,sort_order:this.sortDir});
    const r=await fetch('/api/v1/schedules?'+params,{headers:{Authorization:tok()}});
    if(!r.ok) {
      this.loading=false;
      return;
    }
    this.items=(await r.json()).items.map(item=>({
      ...item,
      previous_active_version:item.active_version,
      pending_active_version:item.active_version,
      savingActiveVersion:false,
      cancelling:false,
    }));
    this.loading=false;
  },
  toggleSort(key){
    if(this.sortBy===key) this.sortDir=this.sortDir==='asc'?'desc':'asc';
    else { this.sortBy=key; this.sortDir='asc'; }
    this.load();
  },
  async del(id){
    if(!confirm('删除该排班记录？将永久删除对应班次和全部排班分配，且无法恢复。')) return;
    const r=await fetch('/api/v1/schedules/'+id,{method:'DELETE',headers:{Authorization:tok()}});
    if(!r.ok){ alert('删除失败: '+await r.text()); }
    await this.load();
  },
  async cancel(item){
    if(!confirm('取消该排班任务？')) return;
    item.cancelling = true;
    try {
      const r=await fetch(`/api/v1/schedules/${item.id}/cancel`,{method:'POST',headers:{Authorization:tok()}});
      if(!r.ok){
        const body=await r.json().catch(()=>({}));
        alert(body.detail||'取消失败');
        return;
      }
      const data=await r.json();
      item.status=data.status;
      item.error_message=data.error_message;
      item.stats=data.stats;
    } finally {
      item.cancelling=false;
    }
  },
  async setActive(item){
    const version=Number(item.pending_active_version);
    if(!version || version===item.previous_active_version) return;
    item.savingActiveVersion=true;
    try {
      const r=await fetch(`/api/v1/schedules/${item.id}/active-version`,{
        method:'PUT',
        headers:{'Content-Type':'application/json',Authorization:tok()},
        body:JSON.stringify({version})
      });
      if(!r.ok){
        const body=await r.json().catch(()=>({}));
        item.pending_active_version=item.previous_active_version;
        alert(body.detail||'生效版本切换失败');
        return;
      }
      const data=await r.json();
      item.active_schedule_id=data.active_schedule_id;
      item.active_version=data.active_version;
      item.previous_active_version=data.active_version;
      item.pending_active_version=data.active_version;
    } finally {
      item.savingActiveVersion=false;
    }
  },
}}
