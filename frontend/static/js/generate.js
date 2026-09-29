function tok(){ return ''; }
function gen(){ return {
  form:{period_start:new Date().toISOString().slice(0,10), period_days:14, long_run:false, solver_config:{timeout_seconds:60,num_workers:4}},
  nurseScope:'all',
  nurses:[], selectedNurseIds:[],
  ruleScope:'all',
  smRules:[], seqRules:[], selectedSmRuleIds:[], selectedSeqRuleIds:[],
  isSuper: localStorage.getItem('user_role') === 'super_admin',
  role: localStorage.getItem('user_role') || '',
  currentUserId:'',
  checked: false,
  tenants:[], targetTenant:'', subByTenant:{},
  nurseTotal:0, nursePage:0, nursePageSize:50, nurseLoadingMore:false,
  smRuleTotal:0, smRulePage:0, smRulePageSize:50, smRuleLoadingMore:false,
  seqRuleTotal:0, seqRulePage:0, seqRulePageSize:50, seqRuleLoadingMore:false,
  get hasRole(){
    // super_admin / tenant_admin / scheduler may generate; viewer/nurse may not.
    return ['super_admin', 'tenant_admin', 'scheduler'].includes(this.role);
  },
  get canGenerate(){
    return this.hasRole && this.subActive && !this.hasNurseSelectionError &&
      this.precheckResult?.can_generate === true;
  },
  get generationBlockReason(){
    const reasons = this.precheckResult?.reasons || [];
    if (this.hasNurseSelectionError) return '手动选择护士时，至少选择一名护士。';
    return reasons.join('；');
  },
  get nurseManual(){
    return this.nurseScope === 'selected';
  },
  get ruleManual(){
    return this.ruleScope === 'selected';
  },
  get hasNurseSelectionError(){
    return this.nurseManual && this.selectedNurseIds.length === 0;
  },
  get nursesByDept(){
    const groups = {};
    for (const n of this.nurses){
      const d = n.department || '未分组';
      (groups[d] = groups[d] || []).push(n);
    }
    return groups;
  },
  get selectedNurseCount(){
    return this.nurseManual
      ? this.selectedNurseIds.length
      : (this.nurseTotal || this.nurses.length);
  },
  get workloadNurseDays(){
    return this.selectedNurseCount * (Number(this.form.period_days) || 0);
  },
  get showBatchHint(){
    return this.workloadNurseDays >= 3000;
  },
  get solverSettingsText(){
    const cfg = this.solverSettings;
    if (!cfg) return '';
    return `求解参数：timeout ${cfg.timeout_seconds}s · workers ${cfg.num_workers}` +
      (cfg.long_run ? ' · long-run' : '');
  },
  get canCancel(){
    if (!this.running || !this.currentRequest) return false;
    if (this.isSuper || this.role === 'tenant_admin') return true;
    return this.role === 'scheduler' && this.currentRequest.requested_by === this.currentUserId;
  },
  seqLabel(r){ return (r.steps||[]).sort((a,b)=>a.position-b.position).map(s=>s.shift_template_name?(s.shift_template_code+' '+s.shift_template_name):'休').join(' → '); },
  selectLoadedNurses(){ this.selectedNurseIds = this.nurses.map(n=>n.id); },
  setNurseScope(scope){
    this.nurseScope = scope;
    this.selectedNurseIds = scope === 'selected'
      ? this.nurses.map(n=>n.id)
      : [];
    this.refreshPrecheck();
  },
  selectLoadedRules(){
    this.selectedSmRuleIds = this.smRules.map(r=>r.id);
    this.selectedSeqRuleIds = this.seqRules.map(r=>r.id);
  },
  setRuleScope(scope){
    this.ruleScope = scope;
    this.selectedSmRuleIds = scope === 'selected' ? this.smRules.map(r=>r.id) : [];
    this.selectedSeqRuleIds = scope === 'selected' ? this.seqRules.map(r=>r.id) : [];
    this.refreshPrecheck();
  },
  get hasMoreNurses(){ return this.nurses.length < this.nurseTotal; },
  get hasMoreSmRules(){ return this.smRules.length < this.smRuleTotal; },
  get hasMoreSeqRules(){ return this.seqRules.length < this.seqRuleTotal; },
  running:false, done:false, error:'', requestId:'', progress:0, statusText:'', message:'', doneStats:'',
  currentRequest:null, cancelPending:false,
  solverSettings:null,
  lastResultId:'',
  lastError:'', lastErrorId:'',
  sub:null, subActive:false, subChecked:false,
  tenantSlug:'',
  maxPeriodDays: 14,
  planLimits: null,
  precheckResult:null, precheckSequence:0, precheckLoading:false,
  get isDemo(){
    if (this.isSuper) { const t = this.tenants.find(x=>x.id===this.targetTenant); return !!(t && t.settings && t.settings.dataset === 'demo'); }
    return this.tenantSlug === 'demo';
  },
  get nurseLimitText(){
    if (!this.planLimits) return '-';
    return this.planLimits.max_nurses === null ? '不限' : this.planLimits.max_nurses;
  },
  async init(){
    // Resolve the authoritative role from the server (localStorage may be stale).
    try {
      const u = await fetchMeCached(tok());
      this.role = u.role;
      this.currentUserId = u.id;
      this.isSuper = u.role === 'super_admin';
      this.tenantSlug = u.tenant_slug || '';
      localStorage.setItem('user_role', u.role);
    } catch (_) {}
    if (this.isSuper) {
      // Super admin: load tenants, restore the last-used tenant (not always
      // the first); subscription/nurses are fetched per selected tenant.
      try {
        const r = await fetch('/api/v1/tenants?page_size=100', {headers:{Authorization:tok()}});
        if (r.ok) {
          this.tenants = (await r.json()).items;
          const saved = localStorage.getItem('gen_target_tenant');
          this.targetTenant = (saved && this.tenants.some(t=>t.id===saved)) ? saved : (this.tenants[0]?.id || '');
        }
      } catch (_) {}
      // Build a tenant_id → is_active map so the dropdown can label each tenant.
      try {
        const r = await fetch('/api/v1/subscriptions', {headers:{Authorization:tok()}});
        if (r.ok) {
          const subs = await r.json();
          this.subByTenant = Object.fromEntries(subs.map(s => [s.tenant_id, s.is_active]));
        }
      } catch (_) {}
      await this.loadTenantData();
    } else {
      await this.loadTenantData();
    }
    this.checked = true;
    this.$watch('selectedNurseIds', () => this.refreshPrecheck());
    this.$watch('selectedSmRuleIds', () => this.refreshPrecheck());
    await this.restoreLast();
  },
  async loadTenantData(){
    this.sub = null; this.subActive = false; this.subChecked = false;
    this.nurseScope = 'all';
    this.nurses = []; this.selectedNurseIds = [];
    this.ruleScope = 'all';
    this.nurseTotal = 0; this.nursePage = 0; this.nurseLoadingMore = false;
    this.smRules = []; this.seqRules = [];
    this.smRuleTotal = 0; this.smRulePage = 0; this.smRuleLoadingMore = false;
    this.seqRuleTotal = 0; this.seqRulePage = 0; this.seqRuleLoadingMore = false;
    this.selectedSmRuleIds = []; this.selectedSeqRuleIds = [];
    const tid = this.isSuper ? this.targetTenant : null;
    if (this.isSuper && tid) localStorage.setItem('gen_target_tenant', tid);
    // Subscription: super admin uses /subscriptions/{tenant_id}; tenant user
    // uses /subscriptions/me.
    try {
    const url = this.isSuper ? `/api/v1/subscriptions/${tid}` : '/api/v1/subscriptions/me';
      const r = await fetch(url, {headers:{Authorization:tok()}});
      if (r.ok) this.sub = await r.json();
    } catch (_) {}
    const limitsUrl = this.isSuper && tid
      ? `/api/v1/subscriptions/${tid}/limits` : '/api/v1/subscriptions/me/limits';
    try {
      const r = await fetch(limitsUrl, {cache:'no-store', headers:{Authorization:tok()}});
      if (r.ok) this.planLimits = await r.json();
    } catch (_) {}
    this.subActive = !!(this.sub && this.sub.is_active);
    this.maxPeriodDays = this.planLimits?.max_period_days ?? null;
    if (this.maxPeriodDays && this.form.period_days > this.maxPeriodDays) this.form.period_days = this.maxPeriodDays;
    this.subChecked = true;
    // Nurses: tenant users are RLS-scoped to their own tenant. Super admin
    // (RLS cleared) sees ALL tenants' nurses, so filter client-side to the
    // selected target tenant. The solver/loader further enforces tenant_id
    // server-side, so non-target nurse_ids would be ignored anyway.
    await Promise.all([
      this.loadNurses(true),
      this.loadSmRules(true),
      this.loadSeqRules(true),
    ]);
    await this.refreshPrecheck();
  },
  async loadNurses(reset=false){
    const page = reset ? 1 : this.nursePage + 1;
    if (!reset) this.nurseLoadingMore = true;
    try {
      const tid = this.isSuper ? this.targetTenant : null;
      let url = `/api/v1/nurses?page=${page}&page_size=${this.nursePageSize}&available_only=true`;
      if (this.isSuper && tid) url += '&tenant_id=' + encodeURIComponent(tid);
      const r = await fetch(url, {headers:{Authorization:tok()}});
      if (!r.ok) return;
      const data = await r.json();
      this.nurses = reset ? data.items : this.nurses.concat(data.items);
      this.nurseTotal = data.total ?? this.nurses.length;
      this.nursePage = data.page ?? page;
    } finally {
      if (!reset) this.nurseLoadingMore = false;
    }
  },
  async loadMoreNurses(){ if (!this.nurseLoadingMore && this.hasMoreNurses) await this.loadNurses(false); },
  precheckPayload(){
    const nurseIds = this.nurseManual ? this.selectedNurseIds : null;
    const selectedRuleIds = this.ruleManual ? this.selectedSmRuleIds : null;
    const payload = {nurse_ids: nurseIds, skill_mix_rule_ids: selectedRuleIds};
    if (this.isSuper) payload.tenant_id = this.targetTenant;
    return payload;
  },
  async refreshPrecheck(){
    if (!this.hasRole || !this.subActive) { this.precheckResult = null; return; }
    if (this.hasNurseSelectionError) { this.precheckResult = null; return; }
    const sequence = ++this.precheckSequence;
    this.precheckLoading = true;
    try {
      const r = await fetch('/api/v1/schedules/generate/precheck', {
        method:'POST',
        headers:{'Content-Type':'application/json', Authorization:tok()},
        body:JSON.stringify(this.precheckPayload()),
      });
      const result = r.ok ? await r.json() : null;
      if (sequence !== this.precheckSequence) return;
      this.precheckResult = result;
    } catch (_) {
      if (sequence === this.precheckSequence) this.precheckResult = null;
    } finally {
      if (sequence === this.precheckSequence) this.precheckLoading = false;
    }
  },
  async loadSmRules(reset=false){
    const page = reset ? 1 : this.smRulePage + 1;
    if (!reset) this.smRuleLoadingMore = true;
    try {
      const tid = this.isSuper ? this.targetTenant : null;
      const tenantQuery = this.isSuper && tid ? '&tenant_id=' + encodeURIComponent(tid) : '';
      const r = await fetch(`/api/v1/skill-mix-rules?page=${page}&page_size=${this.smRulePageSize}${tenantQuery}`, {headers:{Authorization:tok()}});
      if (!r.ok) return;
      const data = await r.json();
      let items = data.items;
      if (this.isSuper && tid) items = items.filter(r=>r.tenant_id===tid);
      this.smRules = reset ? items : this.smRules.concat(items);
      this.smRuleTotal = data.total ?? this.smRules.length;
      this.smRulePage = data.page ?? page;
    } finally {
      if (!reset) this.smRuleLoadingMore = false;
    }
  },
  async loadMoreSmRules(){ if (!this.smRuleLoadingMore && this.hasMoreSmRules) await this.loadSmRules(false); },
  async loadSeqRules(reset=false){
    const page = reset ? 1 : this.seqRulePage + 1;
    if (!reset) this.seqRuleLoadingMore = true;
    try {
      const tid = this.isSuper ? this.targetTenant : null;
      const tenantQuery = this.isSuper && tid ? '&tenant_id=' + encodeURIComponent(tid) : '';
      const r = await fetch(`/api/v1/shift-sequence-rules?page=${page}&page_size=${this.seqRulePageSize}${tenantQuery}`, {headers:{Authorization:tok()}});
      if (!r.ok) return;
      const data = await r.json();
      let items = data.items;
      if (this.isSuper && tid) items = items.filter(r=>r.tenant_id===tid);
      this.seqRules = reset ? items : this.seqRules.concat(items);
      this.seqRuleTotal = data.total ?? this.seqRules.length;
      this.seqRulePage = data.page ?? page;
    } finally {
      if (!reset) this.seqRuleLoadingMore = false;
    }
  },
  async loadMoreSeqRules(){ if (!this.seqRuleLoadingMore && this.hasMoreSeqRules) await this.loadSeqRules(false); },
  async submit(){
    this.error=''; this.running=true; this.progress=5; this.statusText='提交中…'; this.message='';
    if (this.form.long_run) this.message = '大规模排班：可能需要较长时间。';
    const tid = this.isSuper ? this.targetTenant : null;
    const payload = {...this.form, nurse_ids: this.nurseManual ? this.selectedNurseIds : null};
    payload.skill_mix_rule_ids = this.ruleManual ? this.selectedSmRuleIds : null;
    payload.shift_sequence_rule_ids = this.ruleManual ? this.selectedSeqRuleIds : null;
    if (this.isSuper) payload.tenant_id = tid;
    const r=await fetch('/api/v1/schedules/generate',{method:'POST',headers:{'Content-Type':'application/json',Authorization:tok()},body:JSON.stringify(payload)});
    if(!r.ok){ let msg = await r.text(); try { msg = JSON.parse(msg).detail || msg; } catch (_) {} this.error = '❌ 提交失败: ' + msg; this.running=false; return; }
    const req=await r.json(); this.requestId=req.id; localStorage.setItem('last_sched_req', req.id); this.poll();
  },
  async poll(){
    const r=await fetch('/api/v1/schedules/'+this.requestId,{headers:{Authorization:tok()}});
    if(!r.ok){ setTimeout(()=>this.poll(), 1500); return; }
    const req=await r.json();
    this.currentRequest = req;
    // Backfill form fields from the request (so a restored session shows the
    // correct period in the done banner even after a page reload).
    if (req.period_start) this.form.period_start = req.period_start;
    if (req.period_days) this.form.period_days = req.period_days;
    this.solverSettings = req.solver_config || null;
    this.statusText = {pending:'排队中…',running:'求解中…',completed:'完成',failed:'失败',cancelled:'已取消'}[req.status]||req.status;
    this.message = req.error_message || '';
    if(req.status==='completed'){
      this.progress=100; this.running=false; this.done=true;
      this.lastResultId = this.requestId;
      const st = req.stats || {};
      this.doneStats = [
        st.num_assignments !== undefined ? '共 ' + st.num_assignments + ' 个班次安排' : null,
        st.solve_time_seconds !== undefined ? '求解 ' + st.solve_time_seconds + ' 秒' : null,
        req.outcome ? '结果: ' + req.outcome : null,
      ].filter(Boolean).join(' · ');
      return;
    }
    if(req.status==='failed'||req.status==='cancelled'){
      this.running=false;
      this.lastError = req.error_message || req.status;
      this.lastErrorId = this.requestId;
      // Keep last_sched_req so the reason survives a page reload.
      return;
    }
    this.progress = req.status==='running' ? 60 : 20;
    setTimeout(()=>this.poll(), 1500);
  },
  reset(){
    this.done=false; this.running=false; this.error=''; this.requestId=''; this.doneStats=''; this.solverSettings=null;
    this.progress=0; this.statusText='';
    this.lastResultId=''; this.lastError=''; this.lastErrorId='';
    localStorage.removeItem('last_sched_req');
  },
  clearLastResult(){
    this.lastResultId=''; localStorage.removeItem('last_sched_req');
  },
  clearLastError(){
    this.lastError=''; this.lastErrorId=''; localStorage.removeItem('last_sched_req');
  },
  async cancelRequest(){
    if (!this.canCancel || this.cancelPending) return;
    if (!confirm('取消当前排班任务？')) return;
    this.cancelPending = true;
    try {
      const r = await fetch('/api/v1/schedules/' + this.requestId + '/cancel', {method:'POST', headers:{Authorization:tok()}});
      if (!r.ok) {
        let msg = await r.text();
        try { msg = JSON.parse(msg).detail || msg; } catch (_) {}
        this.error = '❌ 取消失败: ' + msg;
        return;
      }
      this.currentRequest = await r.json();
    } finally {
      this.cancelPending = false;
    }
    await this.poll();
  },
  async restoreLast(){
    const rid = localStorage.getItem('last_sched_req');
    if (!rid) return;
    try {
      const r = await fetch('/api/v1/schedules/'+rid, {headers:{Authorization:tok()}});
      if (!r.ok) { localStorage.removeItem('last_sched_req'); return; }
      const req = await r.json();
      if (req.status === 'pending' || req.status === 'running') {
        // Still in progress — resume the progress UI.
        this.requestId = rid;
        this.running = true; this.progress = 5; this.statusText = '恢复中…';
        this.poll();
      } else if (req.status === 'completed') {
        // Done — don't take over the page, just offer a link to the result.
        this.lastResultId = rid;
      } else if (req.status === 'failed' || req.status === 'cancelled') {
        // Show why it didn't finish so the user knows it ran (and failed).
        this.lastError = req.error_message || req.status;
        this.lastErrorId = rid;
      } else {
        localStorage.removeItem('last_sched_req');
      }
    } catch (_) {}
  },
}}
