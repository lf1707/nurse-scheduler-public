function tok(){ return ''; }
function prof(){ return {
  user: {},
  roleNames: {super_admin:'超级管理员', tenant_admin:'租户管理员', scheduler:'排班员', nurse:'护士', viewer:'访客'},
  nameForm: {first_name:'', last_name:''},
  pwForm: {old_password:'', new_password:''},
  siteInfoForm: {contact_email:'', about_text:''},
  nameMsg: '', pwMsg: '', pwOk: false,
  siteInfoMsg: '', siteInfoOk: false,
  mfaStatusMsg: '',
  mfaSetupUri: '', mfaQrSvg: '', mfaConfirmForm: {code: ''},
  mfaRecoveryCodes: [],
  async load(){
    const r = await fetch('/api/v1/auth/me', {headers:{Authorization:tok()}});
    if (!r.ok) return;
    this.user = await r.json();
    const savedCodes = sessionStorage.getItem('new_mfa_recovery_codes');
    if (savedCodes) {
      this.mfaRecoveryCodes = JSON.parse(savedCodes);
      sessionStorage.removeItem('new_mfa_recovery_codes');
      this.mfaStatusMsg = '✅ 恢复码已轮换，请保存新的恢复码';
    }
    this.nameForm.first_name = this.user.first_name;
    this.nameForm.last_name = this.user.last_name;
    this.mfaStatusMsg = this.user.mfa_enabled ? '已启用双因素认证' : '未启用双因素认证';
    if (this.user.role === 'super_admin' && this.user.mfa_enabled) await this.loadSiteInfo();
  },
  async loadSiteInfo(){
    try {
      const r = await fetch('/api/v1/admin/site-info', {headers:{Authorization:tok()}});
      if (r.ok) {
        const d = await r.json();
        this.siteInfoForm.contact_email = d.contact_email || '';
        this.siteInfoForm.about_text = d.about_text || '';
      }
    } catch(_) {}
  },
  async saveSiteInfo(){
    this.siteInfoMsg = ''; this.siteInfoOk = false;
    const r = await fetch('/api/v1/admin/site-info', {
      method:'PUT', headers:{'Content-Type':'application/json', Authorization:tok()},
      body: JSON.stringify({
        contact_email: this.siteInfoForm.contact_email || null,
        about_text: this.siteInfoForm.about_text || null,
      }),
    });
    if (r.ok) { this.siteInfoOk = true; this.siteInfoMsg = '✅ 已保存'; }
    else { const d = await r.json().catch(()=>({})); this.siteInfoMsg = '❌ ' + (d.detail || '保存失败'); }
  },
  async saveName(){
    this.nameMsg = '';
    const r = await fetch('/api/v1/auth/me', {
      method:'PATCH', headers:{'Content-Type':'application/json', Authorization:tok()},
      body: JSON.stringify(this.nameForm),
    });
    if (!r.ok) { const d = await r.json().catch(()=>({})); this.nameMsg = '❌ ' + (d.detail || '保存失败'); return; }
    this.user = await r.json();
    localStorage.setItem('user_display', this.user.tenant_name
      ? `${this.user.first_name}${this.user.last_name} (${this.user.tenant_name})`
      : `${this.user.first_name}${this.user.last_name}`);
    const el = document.getElementById('nav-user');
    if (el) el.textContent = localStorage.getItem('user_display');
    this.nameMsg = '✅ 已保存';
  },
  async savePassword(){
    this.pwMsg = ''; this.pwOk = false;
    const r = await fetch('/api/v1/auth/me/password', {
      method:'POST', headers:{'Content-Type':'application/json', Authorization:tok()},
      body: JSON.stringify(this.pwForm),
    });
    if (r.status === 204) { this.pwOk = true; this.pwMsg = '✅ 密码已修改'; this.pwForm = {old_password:'', new_password:''}; return; }
    const d = await r.json().catch(()=>({}));
    this.pwMsg = '❌ ' + (d.detail || '修改失败');
  },
  async startMfa(){
    this.mfaSetupUri = ''; this.mfaStatusMsg = '';
    const r = await fetch('/api/v1/auth/me/mfa/setup', {
      method:'POST', headers:{Authorization:tok()},
    });
    if (!r.ok) { const d = await r.json().catch(()=>({})); this.mfaStatusMsg = '❌ ' + (d.detail || '设置失败'); return; }
    const d = await r.json();
    this.mfaSetupUri = d.provisioning_uri;
    this.mfaQrSvg = d.qr_code_svg;
  },
  async confirmMfa(){
    this.mfaStatusMsg = ''; this.mfaRecoveryCodes = [];
    const r = await fetch('/api/v1/auth/me/mfa/confirm', {
      method:'POST', headers:{'Content-Type':'application/json', Authorization:tok()},
      body: JSON.stringify(this.mfaConfirmForm),
    });
    if (!r.ok) { const d = await r.json().catch(()=>({})); this.mfaStatusMsg = '❌ ' + (d.detail || '确认失败'); return; }
    const d = await r.json();
    this.mfaRecoveryCodes = d.codes;
    this.user.mfa_enabled = true;
    this.mfaSetupUri = ''; this.mfaQrSvg = '';
    this.mfaConfirmForm = {code: ''};
    this.mfaStatusMsg = '✅ MFA 已启用';
  },
}}
