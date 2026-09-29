function billingPage(){ return {
  tenants: [],
  invoices: [],
  loading: true,
  saving: false,
  error: '',
  notice: '',
  showCreate: false,
  tenantFilter: '',
  statusFilter: '',
  providerFilter: '',
  formTenant: '', formNumber: '', formProvider: 'manual',
  formProviderInvoice: '', formAmountMinor: null, formCurrency: 'USD',
  formStatus: 'issued', formPeriodStart: '', formPeriodEnd: '',
  formDueAt: '', formNotes: '',
  async load(){
    this.loading = true; this.error = '';
    const params = new URLSearchParams();
    if (this.tenantFilter) params.set('tenant_id', this.tenantFilter);
    if (this.statusFilter) params.set('invoice_status', this.statusFilter);
    if (this.providerFilter) params.set('provider', this.providerFilter);
    const query = params.toString();
    try {
      const [tenantsResponse, invoicesResponse] = await Promise.all([
        fetch('/api/v1/tenants?page_size=100', {cache:'no-store'}),
        fetch('/api/v1/billing/invoices' + (query ? `?${query}` : ''), {cache:'no-store'}),
      ]);
      if (!tenantsResponse.ok || !invoicesResponse.ok) {
        throw new Error(await this.errorMessage(tenantsResponse.ok ? invoicesResponse : tenantsResponse));
      }
      this.tenants = (await tenantsResponse.json()).items;
      const invoices = await invoicesResponse.json();
      this.invoices = invoices.map(invoice => ({
        ...invoice,
        tenant_name: this.tenantName(invoice.tenant_id),
        amount_display: this.formatAmount(invoice),
        status_label: this.statusLabel(invoice.status),
        status_class: this.statusClass(invoice.status),
        period_display: this.formatPeriod(invoice),
        due_display: this.formatDate(invoice.due_at),
        paid_display: this.formatDate(invoice.paid_at),
      }));
      if (!this.formTenant && this.tenants.length) this.formTenant = this.tenants[0].id;
    } catch (exception) {
      this.error = exception.message;
    } finally {
      this.loading = false;
    }
  },
  async create(){
    this.error = ''; this.notice = ''; this.saving = true;
    const payload = {
      tenant_id: this.formTenant,
      number: this.formNumber.trim(),
      provider: this.formProvider,
      provider_invoice_id: this.formProviderInvoice.trim() || null,
      amount_minor: this.formAmountMinor,
      currency: this.formCurrency.trim().toUpperCase(),
      status: this.formStatus,
      period_start: this.toDateTime(this.formPeriodStart),
      period_end: this.toDateTime(this.formPeriodEnd),
      due_at: this.toDateTime(this.formDueAt),
      notes: this.formNotes.trim() || null,
    };
    try {
      const response = await fetch('/api/v1/billing/invoices', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify(payload),
      });
      if (!response.ok) throw new Error(await this.errorMessage(response));
      this.notice = '✅ 账单已登记';
      this.resetForm();
      this.showCreate = false;
      await this.load();
    } catch (exception) {
      this.error = exception.message;
    } finally {
      this.saving = false;
    }
  },
  async updateStatus(invoice, nextStatus){
    this.error = ''; this.notice = '';
    try {
      const response = await fetch('/api/v1/billing/invoices/' + encodeURIComponent(invoice.id), {
        method: 'PATCH',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({status: nextStatus}),
      });
      if (!response.ok) throw new Error(await this.errorMessage(response));
      this.notice = nextStatus === 'paid' ? '✅ 账单已标记付款' : '✅ 账单已作废';
      await this.load();
    } catch (exception) {
      this.error = exception.message;
    }
  },
  resetForm(){
    this.formTenant = this.tenantFilter || (this.tenants[0]?.id || '');
    this.formNumber = '';
    this.formProvider = 'manual';
    this.formProviderInvoice = '';
    this.formAmountMinor = null;
    this.formCurrency = 'USD';
    this.formStatus = 'issued';
    this.formPeriodStart = '';
    this.formPeriodEnd = '';
    this.formDueAt = '';
    this.formNotes = '';
  },
  tenantName(tenantId){
    return this.tenants.find(tenant => tenant.id === tenantId)?.name || tenantId;
  },
  statusLabel(status){
    return {issued:'待付款', paid:'已付款', void:'已作废'}[status] || status;
  },
  statusClass(status){
    return {issued:'tag-warn', paid:'tag-ok', void:'tag-fail'}[status] || '';
  },
  formatAmount(invoice){
    const amount = (invoice.amount_minor / 100).toFixed(2);
    return `${invoice.currency} ${amount}`;
  },
  formatPeriod(invoice){
    if (!invoice.period_start && !invoice.period_end) return '—';
    return `${this.formatDate(invoice.period_start)} – ${this.formatDate(invoice.period_end)}`;
  },
  formatDate(value){
    return value ? value.slice(0, 10) : '—';
  },
  toDateTime(date){
    return date ? `${date}T00:00:00Z` : null;
  },
  async errorMessage(response){
    try {
      const payload = await response.json();
      if (typeof payload.detail === 'string') return payload.detail;
      if (Array.isArray(payload.detail)) {
        return payload.detail.map(item => item.msg || JSON.stringify(item)).join('; ');
      }
      if (payload.detail) return JSON.stringify(payload.detail);
      return payload.message || `HTTP ${response.status}`;
    } catch (_) {
      return `HTTP ${response.status}`;
    }
  },
};}
