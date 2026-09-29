function tok(){ return ''; }
function adm(){ return {
  error:'', tenants:[], users:[], selTenant:'', selUser:'',
  impersonating: !!localStorage.getItem('impersonator_name'),
  impName: localStorage.getItem('impersonator_name') || '',
  impMsg:'', createMsg:'', testTenants:[],
  auditEvents:[], auditTotal:0, auditPage:1, auditLoading:false,
  legalHolds:[], legalHoldsLoading:false, legalHoldSaving:false,
  legalHoldForm:{partition_name:'', reason:''}, legalHoldMsg:'',
  auditPartitions:[], auditPartitionsLoading:false,
  legalHoldModal:false,
  auditDownloadLoading:false,
  opsStatus:null, opsLoading:false,
  auditExportLoading:false, auditExportMsg:'', auditTaskStatus:null,
  auditRetentionLoading:false, auditRetentionMsg:'',
  testTenantsEnabled: false,
  testSwitchLoaded: false,
  demoEnabled: false,
  demoCreating: false,
  demoMsg:'',
  cleanupLoading: false,
  cleanupMsg:'',
  switchMsg:'',
  createForm: {
    nurse_count: 6,
    role_count: 2,
    day_group_count: 1,
    shift_count: 3,
    skill_mix_rule_count: 1,
    shift_sequence_rule_count: 1,
    plan: 'pro',
  },
  anomalyScanSchedule: 3600,
  anomalyScanSaving: false,
  anomalyScanRunning: false,
  anomalyScanMsg: '',
  anomalyTaskId: '',
  anomalyTaskLoading: false,
  anomalyTaskStatus: null,
  beatRestarting: false,
  planLimits: {
    free: { max_nurses: 8, max_period_days: 14 },
    pro: { max_nurses: 20, max_period_days: 30 },
    max: { max_nurses: null, max_period_days: null },
    demo: { max_nurses: 10, max_period_days: 14 },
    custom: [],
  },
  savedPlanLimits: null,
  systemPlanDefaults: {
    free: { max_nurses: 8, max_period_days: 14 },
    pro: { max_nurses: 20, max_period_days: 30 },
    max: { max_nurses: null, max_period_days: null },
    demo: { max_nurses: 10, max_period_days: 14 },
  },
  planLimitsSaving: false,
  planLimitsMsg: '',
  get systemPlanKeys(){
    return ['pro', 'free', 'max', 'demo'].filter(plan => !!this.planLimits?.[plan]);
  },
  get planOptions(){
    return this.systemPlanKeys;
  },
  get planLimitSummary(){
    if (!this.planLimits) return '';
    return this.planOptions.map((plan) => {
      const limits = this.planLimits[plan];
      const nurses = limits.max_nurses === null ? '不限' : `最多${limits.max_nurses}`;
      const days = limits.max_period_days === null ? '排班不限' : `排班${limits.max_period_days}天`;
      return `${plan}：${nurses}·${days}`;
    }).join(' · ');
  },
  get selectablePlanOptions(){
    return [...this.planOptions, ...(this.planLimits?.custom || []).map(plan => plan.key)];
  },
  planLabel(plan){
    if (!this.planLimits) return plan;
    const custom = (this.planLimits.custom || []).find(item => item.key === plan);
    const max_nurses = custom ? custom.max_nurses : this.planLimits[plan]?.max_nurses;
    const max_period_days = custom ? custom.max_period_days : this.planLimits[plan]?.max_period_days;
    const nurses = max_nurses === null || max_nurses === undefined ? '不限' : `最多${max_nurses}护士`;
    const days = max_period_days === null || max_period_days === undefined ? '排班不限' : `排班${max_period_days}天`;
    const fixed=plan==='demo'?'·固定':'';
    return `${plan}（${nurses}·${days}${fixed}）`;
  },
  toggleNurseUnlimited(plan){
    const limits = this.planLimits[plan];
    limits.max_nurses = limits.max_nurses === null ? 0 : null;
  },
  togglePeriodUnlimited(plan){
    const limits = this.planLimits[plan];
    limits.max_period_days = limits.max_period_days === null ? 1 : null;
  },
  toggleCustomNurseUnlimited(plan){
    plan.max_nurses = plan.max_nurses === null ? 0 : null;
  },
  toggleCustomPeriodUnlimited(plan){
    plan.max_period_days = plan.max_period_days === null ? 1 : null;
  },
  get deletedSystemPlans(){
    return this.planLimits?.deleted_system || [];
  },
  planConfigSnapshot(data){
    const system = {};
    ['pro', 'free', 'max', 'demo'].forEach(plan => {
      if (data?.[plan]) system[plan] = {...data[plan]};
    });
    return JSON.stringify({
      ...system,
      deleted_system: data?.deleted_system || [],
      custom: (data?.custom || []).map(({key, name, max_nurses, max_period_days, _isNew}) => ({
        key, name, max_nurses, max_period_days, _isNew: Boolean(_isNew),
      })),
    });
  },
  get planLimitsDirty(){
    return this.planConfigSnapshot(this.planLimits) !== this.savedPlanLimits;
  },
  addCustomPlan(){
    this.planLimits.custom.push({
      key: '', name: '', max_nurses: 10, max_period_days: 14, _isNew: true,
    });
  },
  async confirmCustomPlan(plan){
    if (!/^[a-z][a-z0-9_-]{0,29}$/.test(plan.key || '')) {
      this.planLimitsMsg = '❌ 套餐 Key 需以小写字母开头，仅可使用小写字母、数字、- 和 _';
      return;
    }
    if (!plan.name?.trim()) {
      this.planLimitsMsg = '❌ 请填写套餐名称';
      return;
    }
    plan.name = plan.name.trim();
    await this.savePlanLimits();
  },
  removeCustomPlan(index){
    this.planLimits.custom.splice(index, 1);
  },
  removeSystemPlan(plan){
    delete this.planLimits[plan];
    if (!this.planLimits.deleted_system.includes(plan)) {
      this.planLimits.deleted_system.push(plan);
    }
  },
  restoreSystemPlan(plan){
    this.planLimits.deleted_system = this.planLimits.deleted_system.filter(key => key !== plan);
    if (!this.planLimits[plan]) this.planLimits[plan] = {...this.systemPlanDefaults[plan]};
  },
  async load(){
    try {
      await this.loadTenants();
      await this.loadTestTenantSwitch();
      await this.loadOpsStatus();
      await this.loadLegalHolds();
      await this.loadAnomalyScanSchedule();
      await this.loadPlanLimits();
      await this.loadAuditEvents();
    } catch (e) { this.error = e.message; }
    // If currently impersonating, resolve the name from /auth/me
    if (this.impersonating) {
      try {
        const r = await fetch('/api/v1/auth/me', {headers:{Authorization:tok()}});
        if (r.ok) { const u = await r.json(); this.impName = u.email + ' [' + u.role + ']'; }
      } catch (_) {}
    }
  },
  async loadAnomalyScanSchedule(){
    try {
      const r = await fetch('/api/v1/admin/anomaly-scan-schedule', {cache:'no-store', headers:{Authorization:tok()}});
      if (r.ok) { const d = await r.json(); this.anomalyScanSchedule = d.schedule_seconds; }
    } catch (_) {}
  },
  async saveAnomalyScanSchedule(){
    this.anomalyScanMsg = ''; this.anomalyScanSaving = true;
    const r = await fetch('/api/v1/admin/anomaly-scan-schedule', {
      method:'PUT', headers:{'Content-Type':'application/json', Authorization:tok()},
      body:JSON.stringify({schedule_seconds: this.anomalyScanSchedule}),
    });
    this.anomalyScanSaving = false;
    if (r.ok) { this.anomalyScanMsg = '✅ 已保存（重启 beat 后生效）'; }
    else { this.anomalyScanMsg = '❌ ' + await r.text(); }
  },
  async restartBeat(){
    this.beatRestarting = true;
    this.anomalyScanMsg = '';
    try {
      const r = await fetch('/api/v1/admin/ops/restart-beat', {method:'POST', headers:{Authorization:tok()}});
      if (r.ok) { this.anomalyScanMsg = '✅ 已提交重启请求；宿主侧执行完成后生效'; }
      else { this.anomalyScanMsg = '❌ ' + await r.text(); }
    } finally { this.beatRestarting = false; }
  },
  async runAnomalyScan(){
    if (this.anomalyScanRunning) return;
    this.anomalyScanRunning = true;
    this.anomalyScanMsg = '';
    try {
      const r = await fetch('/api/v1/admin/ops/anomaly-scan', {
        method:'POST', headers:{Authorization:tok()},
      });
      if (!r.ok) throw new Error(await this.errorMessage(r));
      const data = await r.json();
      this.anomalyScanMsg = '已提交扫描任务，正在等待结果…';
      await this.pollAnomalyScan(data.task_id);
    } catch (e) {
      this.anomalyScanMsg = '❌ ' + e.message;
    } finally {
      this.anomalyScanRunning = false;
    }
  },
  async queryAnomalyScanTask(){
    const taskId = this.anomalyTaskId.trim();
    if (!taskId) {
      this.anomalyTaskStatus = null;
      this.anomalyScanMsg = '❌ 请输入扫描任务 ID';
      return;
    }
    this.anomalyTaskLoading = true;
    this.anomalyScanMsg = '';
    try {
      await this.pollAnomalyScan(taskId);
    } catch (e) {
      this.anomalyTaskStatus = null;
      this.anomalyScanMsg = '❌ ' + e.message;
    } finally {
      this.anomalyTaskLoading = false;
    }
  },
  async pollAnomalyScan(taskId, attempt=0){
    const r = await fetch('/api/v1/admin/ops/anomaly-scan/tasks/'+encodeURIComponent(taskId), {
      cache:'no-store', headers:{Authorization:tok()},
    });
    if (!r.ok) throw new Error('扫描任务状态查询失败');
    this.anomalyTaskStatus = await r.json();
    this.anomalyTaskId = taskId;
    if (!this.anomalyTaskStatus.ready && attempt < 30) {
      await new Promise(resolve => setTimeout(resolve, 2000));
      await this.pollAnomalyScan(taskId, attempt + 1);
      return;
    }
    if (!this.anomalyTaskStatus.successful) {
      this.anomalyScanMsg = '❌ 扫描失败：' + (this.anomalyTaskStatus.detail || this.anomalyTaskStatus.state);
      return;
    }
    let alerts = [];
    if (this.anomalyTaskStatus.detail) {
      try { alerts = JSON.parse(this.anomalyTaskStatus.detail); } catch (_) {}
    }
    this.anomalyScanMsg = Array.isArray(alerts) && alerts.length
      ? `✅ 扫描完成，触发 ${alerts.length} 条告警：` + alerts.map(item => item.rule).join('、')
      : '✅ 扫描完成，未触发告警。若触发告警，会按配置发送邮件或写入 worker ERROR 日志。';
  },
  async loadPlanLimits(){
    try {
      const r = await fetch('/api/v1/admin/plan-limits', {cache:'no-store', headers:{Authorization:tok()}});
      if (!r.ok) throw new Error(await r.text());
      this.planLimits = await r.json();
      this.savedPlanLimits = this.planConfigSnapshot(this.planLimits);
    } catch (e) {
      this.planLimitsMsg = '❌ 套餐编辑加载失败';
    }
  },
  async savePlanLimits(){
    this.planLimitsMsg = '';
    this.planLimitsSaving = true;
    try {
      const r = await fetch('/api/v1/admin/plan-limits', {
        method:'PUT',
        headers:{'Content-Type':'application/json', Authorization:tok()},
        body:JSON.stringify(this.planLimits),
      });
      if (!r.ok) throw new Error(await r.text());
      this.planLimits = await r.json();
      this.savedPlanLimits = this.planConfigSnapshot(this.planLimits);
      this.planLimitsMsg = '✅ 套餐已保存，立即生效';
    } catch (e) {
      this.planLimitsMsg = '❌ ' + e.message;
    } finally {
      this.planLimitsSaving = false;
    }
  },
  async loadTenants(){
    const r = await fetch('/api/v1/tenants?page_size=100&_t='+Date.now(), {
      cache:'no-store',
      headers:{Authorization:tok()},
    });
    if (!r.ok) throw new Error('需要超级管理员');
    this.tenants = (await r.json()).items;
  },
  formatTime(value){
    if (!value) return '-';
    return new Date(value).toLocaleString();
  },
  async loadOpsStatus(){
    this.opsLoading = true;
    try {
      const r = await fetch('/api/v1/admin/ops/status', {cache:'no-store', headers:{Authorization:tok()}});
      if (!r.ok) throw new Error('运维状态加载失败');
      this.opsStatus = await r.json();
    } catch (e) {
      this.error = e.message;
    } finally {
      this.opsLoading = false;
    }
  },
  async runAuditExport(){
    this.auditExportLoading = true;
    this.auditExportMsg = '';
    this.auditTaskStatus = null;
    try {
      const r = await fetch('/api/v1/admin/audit-exports/run', {
        method:'POST',
        headers:{Authorization:tok()},
      });
      if (!r.ok) throw new Error((await r.json()).detail || '审计归档提交失败');
      const data = await r.json();
      this.auditExportMsg = '已提交后台压缩归档任务，正在等待结果…';
      await this.pollAuditExport(data.task_id);
    } catch (e) {
      this.auditExportMsg = '❌ ' + e.message;
    } finally {
      this.auditExportLoading = false;
    }
  },
  async pollAuditExport(taskId, attempt=0){
    const r = await fetch('/api/v1/admin/audit-exports/tasks/'+encodeURIComponent(taskId), {
      cache:'no-store', headers:{Authorization:tok()},
    });
    if (!r.ok) throw new Error('归档任务状态查询失败');
    this.auditTaskStatus = await r.json();
    if (!this.auditTaskStatus.ready && attempt < 30) {
      await new Promise(resolve => setTimeout(resolve, 2000));
      await this.pollAuditExport(taskId, attempt + 1);
      return;
    }
    if (this.auditTaskStatus.successful) {
      this.auditExportMsg = '✅ 压缩归档完成。';
      await Promise.all([this.loadOpsStatus(), this.loadAuditEvents()]);
    } else if (this.auditTaskStatus.ready) {
      this.auditExportMsg = '❌ 压缩归档失败：' + (this.auditTaskStatus.detail || this.auditTaskStatus.state);
    } else {
      this.auditExportMsg = '⏳ 压缩归档仍在后台执行，可稍后刷新状态。';
    }
  },
  async runAuditRetention(){
    if (!window.confirm('确认按保留策略清理已满足条件的审计分区？此操作不可恢复。')) return;
    this.auditRetentionLoading = true;
    this.auditRetentionMsg = '';
    try {
      const r = await fetch('/api/v1/admin/audit-retention/run', {
        method:'POST',
        headers:{Authorization:tok()},
      });
      if (!r.ok) throw new Error((await r.json()).detail || '保留清理提交失败');
      const data = await r.json();
      this.auditRetentionMsg = '已提交后台保留清理任务，正在等待结果…';
      await this.pollAuditRetention(data.task_id);
    } catch (e) {
      this.auditRetentionMsg = '❌ ' + e.message;
    } finally {
      this.auditRetentionLoading = false;
    }
  },
  async pollAuditRetention(taskId, attempt=0){
    const r = await fetch('/api/v1/admin/audit-exports/tasks/'+encodeURIComponent(taskId), {
      cache:'no-store', headers:{Authorization:tok()},
    });
    if (!r.ok) throw new Error('保留清理任务状态查询失败');
    const status = await r.json();
    if (!status.ready && attempt < 30) {
      await new Promise(resolve => setTimeout(resolve, 2000));
      await this.pollAuditRetention(taskId, attempt + 1);
      return;
    }
    if (status.successful) {
      let expired = [];
      try { expired = JSON.parse(status.detail || '[]'); } catch {}
      this.auditRetentionMsg = expired.length
        ? '✅ 保留清理完成，已删除：' + expired.join(', ')
        : '✅ 保留检查完成，当前没有满足安全条件的可删除分区。';
      await Promise.all([this.loadOpsStatus(), this.loadAuditEvents()]);
    } else if (status.ready) {
      this.auditRetentionMsg = '❌ 保留清理失败：' + (status.detail || status.state);
    } else {
      this.auditRetentionMsg = '⏳ 保留清理仍在后台执行，可稍后刷新状态。';
    }
  },
  async loadAuditEvents(){
    this.auditLoading = true;
    try {
      const r = await fetch('/api/v1/admin/audit-events?page='+this.auditPage+'&page_size=50', {
        cache:'no-store', headers:{Authorization:tok()},
      });
      if (!r.ok) throw new Error('安全审计加载失败');
      const data = await r.json();
      this.auditEvents = data.items;
      this.auditTotal = data.total;
    } catch (e) {
      this.error = e.message;
    } finally {
      this.auditLoading = false;
    }
  },
  async loadLegalHolds(){
    this.legalHoldsLoading = true;
    try {
      const r = await fetch('/api/v1/admin/audit-legal-holds', {
        cache:'no-store', headers:{Authorization:tok()},
      });
      if (!r.ok) throw new Error('审计 legal hold 加载失败');
      this.legalHolds = await r.json();
    } catch (e) {
      this.legalHoldMsg = '❌ ' + e.message;
    } finally {
      this.legalHoldsLoading = false;
    }
  },
  openLegalHoldForm(){
    this.legalHoldForm = {partition_name:'', reason:''};
    this.legalHoldMsg = '';
    this.legalHoldModal = true;
    if (!this.$refs.legalHoldModal.open) this.$refs.legalHoldModal.showModal();
    this.$refs.legalHoldReason.value = '';
    this.syncLegalHoldControls();
    this.loadAuditPartitions();
  },
  closeLegalHoldForm(){
    this.legalHoldModal = false;
    if (this.$refs.legalHoldModal.open) this.$refs.legalHoldModal.close();
  },
  async loadAuditPartitions(){
    this.auditPartitionsLoading = true;
    this.syncLegalHoldControls();
    try {
      const r = await fetch('/api/v1/admin/audit-partitions', {
        cache:'no-store', headers:{Authorization:tok()},
      });
      if (!r.ok) throw new Error('审计分区加载失败');
      this.auditPartitions = await r.json();
    } catch (e) {
      this.legalHoldMsg = '❌ ' + e.message;
    } finally {
      this.auditPartitionsLoading = false;
      this.syncLegalHoldControls();
      setTimeout(() => this.syncLegalHoldControls(), 0);
    }
  },
  syncLegalHoldControls(){
    const select = this.$refs.legalHoldPartitionSelect;
    const selected = this.legalHoldForm.partition_name;
    select.replaceChildren(
      new Option('请选择分区…', ''),
      ...this.auditPartitions.map(partition => new Option(partition, partition)),
    );
    select.value = this.auditPartitions.includes(selected) ? selected : '';
    select.disabled = this.auditPartitionsLoading || !this.auditPartitions.length;
    this.$refs.legalHoldSubmit.disabled = (
      this.legalHoldSaving
      || !this.legalHoldForm.partition_name
      || !this.legalHoldForm.reason.trim()
    );
  },
  async createLegalHold(){
    this.legalHoldMsg = '';
    this.legalHoldSaving = true;
    this.syncLegalHoldControls();
    try {
      const r = await fetch('/api/v1/admin/audit-legal-holds', {
        method:'POST',
        headers:{'Content-Type':'application/json', Authorization:tok()},
        body:JSON.stringify(this.legalHoldForm),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || 'Legal hold 创建失败');
      this.legalHoldForm = {partition_name:'', reason:''};
      this.syncLegalHoldControls();
      this.legalHoldMsg = '✅ Legal hold 已创建。';
      this.legalHoldModal = false;
      if (this.$refs.legalHoldModal.open) this.$refs.legalHoldModal.close();
      await this.loadLegalHolds();
    } catch (e) {
      this.legalHoldMsg = '❌ ' + e.message;
    } finally {
      this.legalHoldSaving = false;
      this.syncLegalHoldControls();
    }
  },
  async deleteLegalHold(hold){
    if (!window.confirm(`确认解除 ${hold.partition_name} 的 legal hold？解除后该分区可在满足条件时被清理。`)) return;
    this.legalHoldMsg = '';
    try {
      const r = await fetch('/api/v1/admin/audit-legal-holds/' + hold.id, {
        method:'DELETE', headers:{Authorization:tok()},
      });
      if (!r.ok) {
        const data = await r.json().catch(() => ({}));
        throw new Error(data.detail || 'Legal hold 解除失败');
      }
      this.legalHoldMsg = '✅ Legal hold 已解除。';
      await this.loadLegalHolds();
    } catch (e) {
      this.legalHoldMsg = '❌ ' + e.message;
    }
  },
  async downloadAuditEvents(){
    this.auditDownloadLoading = true; this.error = '';
    try {
      const r = await fetch('/api/v1/admin/audit-events/export.csv?limit=50000', {
        cache:'no-store', headers:{Authorization:tok()},
      });
      if (!r.ok) throw new Error('安全审计下载失败');
      const blob = await r.blob();
      const disposition = r.headers.get('Content-Disposition') || '';
      const match = disposition.match(/filename="([^"]+)"/);
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = match?.[1] || 'security-audit.csv';
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(link.href);
    } catch (e) {
      this.error = e.message;
    } finally {
      this.auditDownloadLoading = false;
    }
  },
  async loadTestTenantSwitch(){
    const r = await fetch('/api/v1/admin/test-tenant/enabled', {cache:'no-store', headers:{Authorization:tok()}});
    if (!r.ok) {
      this.testSwitchLoaded = true;
      this.error = '仿真测试开关状态加载失败';
      return;
    }
    const data = await r.json();
    this.testTenantsEnabled = data.enabled;
    this.demoEnabled = data.demo_enabled;
    this.testSwitchLoaded = true;
    if (data.enabled) await this.loadTestTenants();
  },
  async toggleTestTenants(){
    const target = this.testTenantsEnabled;
    this.switchMsg = '';
    const r = await fetch('/api/v1/admin/test-tenant/enabled', {
      method:'PUT',
      headers:{'Content-Type':'application/json', Authorization:tok()},
      body:JSON.stringify({enabled:target}),
    });
    if (!r.ok) {
      this.testTenantsEnabled = !target;
      this.switchMsg = '❌ ' + (await r.text());
      return;
    }
    const data = await r.json();
    this.testTenantsEnabled = data.enabled;
    this.switchMsg = data.enabled ? '已开启' : '已关闭';
    if (!data.enabled) this.testTenants = [];
    else await this.loadTestTenants();
  },
  async createDemoTenant(){
    if(this.demoCreating) return;
    this.demoCreating = true;
    this.demoMsg = '';
    try {
      const r = await fetch('/api/v1/admin/test-tenant/demo', {
        method:'POST',
        headers:{Authorization:tok()},
      });
      if (!r.ok) throw new Error(await this.errorMessage(r));
      const data = await r.json();
      this.demoMsg = `✅ Demo 租户已就绪：${data.slug} / ${data.admin_email} · 密码由环境变量 DEMO_TENANT_PASSWORD 配置 · 固定 demo 套餐`;
      await this.loadTestTenants();
    } catch (e) {
      this.demoMsg = '❌ ' + e.message;
    } finally {
      this.demoCreating = false;
    }
  },
  async errorMessage(response, fallback='请求失败'){
    const text = await response.text().catch(() => '');
    try { const body = JSON.parse(text); return body.detail || fallback; }
    catch(_) { return text || fallback; }
  },
  async loadTestTenants(){
    const r = await fetch('/api/v1/admin/test-tenant?_t='+Date.now(), {cache:'no-store', headers:{Authorization:tok()}});
    if (!r.ok) {
      this.error = '测试租户记录加载失败';
      return;
    }
    this.testTenants = await r.json();
  },
  async loadUsers(){
    this.users = []; this.selUser = '';
    if (!this.selTenant) return;
    const r = await fetch('/api/v1/admin/tenants/' + this.selTenant + '/users', {headers:{Authorization:tok()}});
    if (r.ok) this.users = await r.json();
  },
  async impersonate(){
    this.impMsg = '';
    const r = await fetch('/api/v1/auth/impersonate/' + this.selUser, {method:'POST', headers:{Authorization:tok()}});
    if (!r.ok) { this.impMsg = '❌ ' + (await r.text()); return; }
    const data = await r.json();
    localStorage.setItem('impersonator_name', '超级管理员');
    void data;
    this.impersonating = true;
    // Go to dashboard as the impersonated user.
    location.href = '/pages/dashboard';
  },
  exitImpersonate(){
    fetch('/api/v1/auth/impersonation-exit', {method:'POST'})
      .catch(() => {});
    localStorage.removeItem('impersonator_name');
    location.href = '/pages/admin';
  },
  async createTest(){
    this.createMsg = '';
    const r = await fetch('/api/v1/admin/test-tenant', {
      method:'POST',
      headers:{'Content-Type':'application/json', Authorization:tok()},
      body:JSON.stringify(this.createForm),
    });
    if (!r.ok) { this.createMsg = '❌ ' + (await r.text()); return; }
    const t = await r.json();
    this.createMsg = t.admin_password
      ? `✅ 已创建 ${t.slug} · 管理员 ${t.admin_email} / 密码 ${t.admin_password}（仅显示一次，请立即保存）`
      : '✅ 已创建 ' + t.slug;
    await this.loadTenants().catch(()=>{});
    await this.loadTestTenants();
    this.testTenants = [...this.testTenants];
  },
  async cleanupTestData(){
    if (this.cleanupLoading) return;
    if (!confirm('确认清理测试数据？将删除测试租户、测试申请和孤儿数据；Demo 租户会保留。')) return;
    this.cleanupLoading = true;
    this.cleanupMsg = '';
    try {
      const r = await fetch('/api/v1/admin/test-tenant/cleanup', {
        method:'POST', headers:{Authorization:tok()},
      });
      if (!r.ok) throw new Error(await this.errorMessage(r));
      const data = await r.json();
      this.cleanupMsg = `✅ 已清理 ${data.deleted_test_tenants.length} 个测试租户、${data.deleted_test_applications} 条测试申请、${data.total_deleted_orphan_rows} 条孤儿数据`;
      await this.loadTenants().catch(() => {});
      await this.loadTestTenants();
    } catch (e) {
      this.cleanupMsg = '❌ ' + e.message;
    } finally {
      this.cleanupLoading = false;
    }
  },
  async loginTest(t){
    // Test credentials are shown only once. The console can still enter a
    // tenant through the audited super-admin impersonation endpoint.
    let d;
    const r = await fetch('/api/v1/auth/impersonate/' + t.admin_user_id, {
      method:'POST', headers:{Authorization:tok()},
    });
    if (!r.ok) { this.createMsg = '❌ 登录失败: ' + (await r.text()); return; }
    d = await r.json();
    localStorage.setItem('impersonator_name', '超级管理员');
    void d;
    location.href = '/pages/dashboard';
  },
}}
