function tok() { return ''; }
function tenantApplicationsPage() {
  return {
    rows: [], selected: null, loading: true, error: '', notice: '',
    total: 0, reviewNotes: '', rejectReason: '',
    autoApprove: false, autoPlan: 'free', autoApproveSaving: false,
    autoApproveNotice: '', autoApproveError: '',
    filter: {status: 'pending_review', page: 1, page_size: 20},
    async load() {
      await this.loadAutoApprove();
      this.loading = true; this.error = ''; this.notice = '';
      const params = new URLSearchParams({
        page: this.filter.page, page_size: this.filter.page_size,
      });
      if (this.filter.status) params.set('status', this.filter.status);
      try {
        const response = await fetch('/api/v1/tenant-applications?' + params, {
          cache: 'no-store', headers: {Authorization: tok()},
        });
        if (!response.ok) throw new Error(await this.message(response, '加载失败'));
        const body = await response.json();
        this.rows = body.items; this.total = body.total;
        if (!this.rows.some(row => row.id === this.selected?.id)) this.select(null);
      } catch (error) { this.error = error.message; }
      this.loading = false;
    },
    async loadAutoApprove() {
      try {
        const response = await fetch('/api/v1/admin/auto-approve', {
          cache: 'no-store', headers: {Authorization: tok()},
        });
        if (!response.ok) return;
        const body = await response.json();
        this.autoApprove = body.auto_approve;
        this.autoPlan = body.auto_plan || 'free';
      } catch (_) {}
    },
    async saveAutoApprove() {
      this.autoApproveSaving = true;
      this.autoApproveNotice = ''; this.autoApproveError = '';
      try {
        const response = await fetch('/api/v1/admin/auto-approve', {
          method: 'PUT',
          headers: {'Content-Type': 'application/json', Authorization: tok()},
          body: JSON.stringify({auto_approve: this.autoApprove, auto_plan: this.autoPlan}),
        });
        if (!response.ok) throw new Error(await this.message(response, '保存失败'));
        this.autoApproveNotice = '设置已保存';
      } catch (error) { this.autoApproveError = error.message; }
      this.autoApproveSaving = false;
    },
    select(row) {
      this.selected = row; this.reviewNotes = ''; this.rejectReason = '';
      if (row) {
        if (row.review_notes) this.reviewNotes = row.review_notes;
        if (row.rejection_reason) this.rejectReason = row.rejection_reason;
      }
    },
    canReview() { return this.selected?.status === 'pending_review'; },
    canResend() { return this.selected?.status === 'approved' && !this.selected?.setup_completed_at; },
    actionHint() {
      const application = this.selected;
      if (!application) return '';
      if (application.status === 'pending_review') return '该申请等待审核，可批准或拒绝。';
      if (application.status === 'approved') {
        return application.setup_completed_at
          ? '管理员已完成初始化，无需重发邀请。'
          : '管理员尚未完成初始化，可重发邀请。';
      }
      if (application.status === 'rejected') return '申请已拒绝，无进一步审批操作。';
      if (application.status === 'expired') return '申请已过期，请让申请方重新提交。';
      return '';
    },
    async approve() {
      if (!this.selected || !confirm(`确定批准「${this.selected.organization_name}」并创建租户？`)) return;
      await this.decide('approve', {review_notes: this.reviewNotes || null});
    },
    async reject() {
      if (!this.selected || (this.rejectReason || '').trim().length < 5) {
        this.error = '请填写至少 5 个字符的拒绝理由'; return;
      }
      if (!confirm('确定拒绝该申请？')) return;
      await this.decide('reject', {
        reason: this.rejectReason.trim(), review_notes: this.reviewNotes || null,
      });
    },
    async decide(action, body) {
      this.error = ''; this.notice = '';
      const response = await fetch(`/api/v1/tenant-applications/${this.selected.id}/${action}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json', Authorization: tok()},
        body: JSON.stringify(body),
      });
      if (!response.ok) { this.error = await this.message(response); return; }
      const result = await response.json();
      this.selected = result.application;
      this.notice = action === 'approve'
        ? (result.email_sent ? '申请已批准，邀请邮件已发送' : '申请已批准，但邀请邮件发送失败')
        : (result.email_sent ? '申请已拒绝，结果邮件已发送' : '申请已拒绝，但结果邮件发送失败');
      await this.load();
      this.select(result.application);
    },
    async resendInvitation() {
      this.error = ''; this.notice = '';
      const response = await fetch(`/api/v1/tenant-applications/${this.selected.id}/resend-invitation`, {
        method: 'POST', headers: {Authorization: tok()},
      });
      if (!response.ok) { this.error = await this.message(response); return; }
      const result = await response.json();
      this.selected = result.application;
      this.notice = result.email_sent ? '邀请邮件已重发' : '邀请邮件发送失败';
    },
    previousPage() { if (this.filter.page > 1) { this.filter.page--; this.load(); } },
    nextPage() { if (this.hasNextPage()) { this.filter.page++; this.load(); } },
    hasNextPage() { return this.filter.page * this.filter.page_size < this.total; },
    pageLabel() { return `${this.filter.page} / ${Math.max(1, Math.ceil(this.total / this.filter.page_size))}`; },
    statusText(value) {
      return {
        pending_email_verification: '待邮箱验证', pending_review: '待审核',
        approved: '已批准', rejected: '已拒绝', expired: '已过期',
      }[value] || value;
    },
    statusClass(value) {
      return {
        pending_email_verification: 'tag-run', pending_review: 'tag-run',
        approved: 'tag-ok', rejected: 'tag-fail', expired: 'tag-fail',
      }[value] || '';
    },
    formatTime(value) { return value ? value.replace('T', ' ').slice(0, 16) : '—'; },
    async message(response, fallback = '请求失败') {
      const text = await response.text().catch(() => '');
      try { return JSON.parse(text).detail || fallback; } catch (_) { return text || fallback; }
    },
  };
}
