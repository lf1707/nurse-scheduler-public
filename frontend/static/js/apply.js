function applyPage() {
  return {
    startedAt: 0,
    submitted: false,
    loading: false,
    error: '',
    form: {
      organization_name: '', contact_name: '', contact_email: '',
      country: '', timezone: Intl.supportedValuesOf ? Intl.supportedValuesOf('timeZone')[0] : '',
      expected_nurse_count: 10, use_case_summary: '', honeypot: '',
    },
    init() {
      this.startedAt = Date.now();
      try {
        this.form.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
      } catch (_) { this.form.timezone = 'UTC'; }
    },
    reset() {
      this.form = {
        organization_name: '', contact_name: '', contact_email: '',
        country: '', timezone: this.form.timezone,
        expected_nurse_count: 10, use_case_summary: '', honeypot: '',
      };
      this.startedAt = Date.now();
      this.error = '';
    },
    payload() {
      return {...this.form, form_started_at: this.startedAt};
    },
    async submit() {
      this.loading = true; this.error = '';
      try {
        const response = await fetch('/api/v1/tenant-applications', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(this.payload()),
        });
        if (!response.ok) throw new Error(await this.message(response));
        this.submitted = true;
      } catch (error) { this.error = error.message; }
      this.loading = false;
    },
    async resend() {
      this.error = '';
      try {
        const response = await fetch('/api/v1/tenant-applications/resend', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({email: this.form.contact_email}),
        });
        if (!response.ok) throw new Error(await this.message(response));
        this.error = '';
      } catch (error) { this.error = error.message; }
    },
    async message(response) {
      const text = await response.text().catch(() => '');
      try { return JSON.parse(text).detail || text || '请求失败'; }
      catch (_) { return text || '请求失败'; }
    },
  };
}
