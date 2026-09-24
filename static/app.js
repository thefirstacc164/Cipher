/* ============================================================
   CIPHER v1.2.0 — Frontend
   Solo project by Stepundrik
   ============================================================ */

// ==========  STATE  ==========
let currentUser = null;
let currentConv = null;
let currentConvMeta = null;
let pollTimer = null;
let typingSentAt = 0;
let pendingImage = null;
let dismissedAnns = JSON.parse(localStorage.getItem('cipher_dismissed_anns') || '[]');
let pollConfig = {
  active_ms: 3000,
  idle_ms: 8000,
  very_idle_ms: 15000,
  hidden_ms: 30000,
  idle_after_cycles: 5,
  very_idle_after_cycles: 20
};
let idleCycles = 0;
let msgListHash = '';
let convListHash = '';
let convListTimer = null;
let sessionCheckWarnedOnce = false;

// v1.2 state
let badgeCatalog = {};          // badge_key -> {label, icon, tip, color}
let decryptedPreview = {};      // message id -> plaintext, for the sidebar
let messageCache = {};          // conversation id -> [decrypted messages]
let lastMessageStamp = {};      // conversation id -> newest created_at seen
let notificationTimer = null;
let encryptionReady = false;
let shopCurrency = 'shards';
let lastSorryState = null;

// ==========  UTILITIES  ==========
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
}

function toast(msg, type = 'info', duration = 4000) {
  const box = document.getElementById('toast-container');
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  t.textContent = msg;
  box.appendChild(t);
  setTimeout(() => {
    t.classList.add('fade-out');
    setTimeout(() => t.remove(), 300);
  }, duration);
}

async function api(url, opts = {}) {
  const options = Object.assign({
    headers: { 'Content-Type': 'application/json' }
  }, opts);
  if (options.body && typeof options.body !== 'string') {
    options.body = JSON.stringify(options.body);
  }
  try {
    const r = await fetch(url, options);
    const j = await r.json().catch(() => ({}));

    // Session enforcement: if backend says logout, force reload
    if (r.status === 401 || r.status === 403) {
      if (j.logout || j.suspended || j.banned) {
        if (!sessionCheckWarnedOnce) {
          sessionCheckWarnedOnce = true;
          toast(j.error || 'Session ended', 'error', 5000);
          setTimeout(() => location.reload(), 1500);
        }
      }
    }
    return { ok: r.ok, status: r.status, data: j };
  } catch (err) {
    return { ok: false, status: 0, data: { error: 'Network error' } };
  }
}

function showModal(html, wide = false, tall = false) {
  const modal = document.getElementById('modal');
  const bg = document.getElementById('modal-bg');
  modal.classList.toggle('wide', wide);
  modal.classList.toggle('tall', tall);
  document.getElementById('modal-content').innerHTML = html;
  bg.classList.remove('hidden');
}

function closeModal() {
  document.getElementById('modal-bg').classList.add('hidden');
  document.getElementById('modal-content').innerHTML = '';
}

document.getElementById('modal-bg').addEventListener('click', (e) => {
  if (e.target.id === 'modal-bg') closeModal();
});

function showProfileCard(html) {
  document.getElementById('profile-card-content').innerHTML = html;
  document.getElementById('profile-card-bg').classList.remove('hidden');
}
function closeProfileCard() {
  document.getElementById('profile-card-bg').classList.add('hidden');
  document.getElementById('profile-card-content').innerHTML = '';
}

function updateTheme(hex) {
  if (!hex || !/^#[0-9a-f]{6}$/i.test(hex)) hex = '#00d9ff';
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  const root = document.documentElement.style;
  root.setProperty('--accent', hex);
  const darken = v => Math.max(0, Math.floor(v * 0.85));
  const acc2 = `#${darken(r).toString(16).padStart(2, '0')}${darken(g).toString(16).padStart(2, '0')}${darken(b).toString(16).padStart(2, '0')}`;
  root.setProperty('--accent-2', acc2);
  root.setProperty('--accent-glow', `rgba(${r},${g},${b},0.35)`);
  root.setProperty('--accent-soft', `rgba(${r},${g},${b},0.1)`);
  const brightness = (r * 299 + g * 587 + b * 114) / 1000;
  root.setProperty('--accent-ink', brightness > 160 ? '#001820' : '#ffffff');
}

function formatTime(iso) {
  try {
    return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch { return ''; }
}
function formatShortTime(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    const now = new Date();
    if (d.toDateString() === now.toDateString()) {
      return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
  } catch { return ''; }
}
function formatDayLabel(iso) {
  try {
    const d = new Date(iso);
    const now = new Date();
    const y = new Date(now); y.setDate(y.getDate() - 1);
    if (d.toDateString() === now.toDateString()) return 'Today';
    if (d.toDateString() === y.toDateString()) return 'Yesterday';
    return d.toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' });
  } catch { return ''; }
}
function formatDate(iso) {
  try {
    return new Date(iso).toLocaleDateString([], { month: 'short', year: 'numeric' });
  } catch { return ''; }
}

// One avatar renderer, used everywhere, so effects show up in the sidebar,
// the chat header, messages, the leaderboard and profile cards alike (BUG 4).
function avatarHtml(user, size = 'md', clickable = false) {
  if (!user) return `<div class="avatar ${size}">?</div>`;
  const effects = (user.active_effects || []).join(' ');
  const bgColor = user.nickname_color || '#00d9ff';
  const style = `background: linear-gradient(135deg, ${bgColor}, #6366f1)`;
  const letter = (user.username || '?')[0].toUpperCase();
  const click = clickable && user.username
    ? ` onclick="event.stopPropagation(); showUserProfile('${esc(user.username)}')" style="cursor:pointer;${style}"`
    : ` style="${style}"`;
  if (user.avatar_url) {
    return `<div class="avatar ${size} ${effects}"${click}><img src="${esc(user.avatar_url)}" alt=""></div>`;
  }
  return `<div class="avatar ${size} ${effects}"${click}>${esc(letter)}</div>`;
}

function badgeMeta(key) {
  const k = String(key || '').replace(/^badge-/, '');
  return badgeCatalog[k] || { label: k, icon: '🏅' };
}

// Badges with a real label, icon and hover tooltip (BUG 3). Max 5 inline;
// the rest collapse into a "+N more" chip that opens the full grid.
function badgesHtml(activeBadges, opts = {}) {
  const list = (activeBadges || []).filter(Boolean);
  if (!list.length) return '';
  const max = opts.max || 5;
  const shown = list.slice(0, max);
  const rest = list.slice(max);
  const chips = shown.map(b => {
    const meta = badgeMeta(b);
    const key = String(b).replace(/^badge-/, '');
    const tip = meta.tip ? ` — ${esc(meta.tip)}` : '';
    return `<span class="user-badge badge-${esc(key)}" title="${esc(meta.label)}${tip}"
      ${meta.color ? `style="--badge-color:${esc(meta.color)}"` : ''}>${esc(meta.icon || '')}<span class="badge-label">${esc(meta.label)}</span></span>`;
  }).join('');
  const more = rest.length
    ? `<button class="user-badge more" type="button" onclick="event.stopPropagation(); showAllBadges(${esc(JSON.stringify(list))})">+${rest.length} more</button>`
    : '';
  return `<span class="badge-strip ${opts.size === 'lg' ? 'lg' : ''}">${chips}${more}</span>`;
}

function showAllBadges(list) {
  const items = (list || []).map(b => {
    const meta = badgeMeta(b);
    const key = String(b).replace(/^badge-/, '');
    return `<div class="badge-grid-item badge-${esc(key)}">
      <div class="badge-grid-icon">${esc(meta.icon || '🏅')}</div>
      <div><div class="badge-grid-name">${esc(meta.label)}</div>
      <div class="badge-grid-tip">${esc(meta.tip || '')}</div></div>
    </div>`;
  }).join('');
  showModal(`<h3>🏅 <span class="accent">Badges</span></h3><div class="badge-grid">${items}</div>`, true);
}

// ==========  AUTH SCREEN  ==========
function initAuthTabs() {
  document.querySelectorAll('.auth-card .tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.auth-card .tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      const isSignup = tab.dataset.tab === 'signup';
      document.getElementById('invite-field').classList.toggle('hidden', !isSignup);
      document.getElementById('affiliate-field').classList.toggle('hidden', !isSignup);
      document.getElementById('tos-field').classList.toggle('hidden', !isSignup);
      document.getElementById('totp-field').classList.add('hidden');
      document.getElementById('auth-submit').textContent = isSignup ? 'Create account' : 'Sign in';
      document.getElementById('auth-error').textContent = '';
    });
  });

  const params = new URLSearchParams(window.location.search);
  const inv = params.get('invite');
  const aff = params.get('affiliate');
  if (inv || aff) {
    document.querySelector('.tab[data-tab="signup"]').click();
    if (inv) document.getElementById('auth-invite').value = inv;
    if (aff) document.getElementById('auth-affiliate').value = aff;
  }
}

document.getElementById('auth-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const errBox = document.getElementById('auth-error');
  errBox.textContent = '';
  errBox.className = 'form-msg';

  const isSignup = document.querySelector('.auth-card .tab.active').dataset.tab === 'signup';

  if (isSignup && !document.getElementById('auth-tos-check').checked) {
    errBox.textContent = 'You must accept the Terms of Service';
    return;
  }

  const body = {
    username: document.getElementById('auth-username').value.trim(),
    password: document.getElementById('auth-password').value
  };
  if (isSignup) {
    const inv = document.getElementById('auth-invite').value.trim();
    const aff = document.getElementById('auth-affiliate').value.trim();
    if (inv) body.invite_code = inv;
    if (aff) body.affiliate_code = aff;
    body.accepted_tos = true;
  } else {
    const totp = document.getElementById('auth-totp').value.trim();
    if (totp) body.totp = totp;
  }

  const btn = document.getElementById('auth-submit');
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = '...';

  const res = await api(isSignup ? '/api/signup' : '/api/login', {
    method: 'POST',
    body
  });

  btn.disabled = false;
  btn.textContent = originalText;

  if (!isSignup && res.data.needs_2fa) {
    document.getElementById('totp-field').classList.remove('hidden');
    document.getElementById('auth-totp').focus();
    errBox.textContent = res.data.error || 'Enter your 6-digit code';
    return;
  }

  if (!res.ok) {
    errBox.textContent = res.data.error || 'Something went wrong';
    return;
  }

  currentUser = res.data.user;
  pendingUnlockPassword = body.password;

  // The auth routes return the full /api/me payload now, but if a slim one
  // ever comes back (old cache, rollback), fetch the real thing so the app
  // never paints with shards/cores stuck at 0 until a manual reload.
  if (!currentUser || currentUser.shards === undefined) {
    const meRes = await api('/api/me');
    if (meRes.ok && meRes.data.user) currentUser = meRes.data.user;
  }

  if (isSignup && res.data.recovery_phrase) {
    showRecoveryModal(res.data.recovery_phrase, res.data.recovery_key);
  } else {
    enterApp();
  }
});

// ==========  RECOVERY MODAL (after signup)  ==========
function showRecoveryModal(phrase, key) {
  const html = `
    <h3>🔐 Save your <span class="accent">recovery info</span></h3>
    <p>You need both of these to reset your password if you ever lose access. Save them somewhere safe. This is your only chance.</p>

    <div class="recovery-box">
      <div class="recovery-title">Recovery phrase (12 words)</div>
      <div class="recovery-desc">Write these down or store in a password manager.</div>
      <div class="recovery-content">${esc(phrase)}</div>
      <button class="btn-mini ghost" onclick="copyText('${esc(phrase)}', this)">Copy phrase</button>
    </div>

    <div class="recovery-box">
      <div class="recovery-title">Recovery key file</div>
      <div class="recovery-desc">Download this file and keep it safe on your computer.</div>
      <button class="btn primary" onclick="downloadKey('${esc(currentUser.username)}', '${esc(key)}')">Download recovery file</button>
    </div>

    <div class="setting-row">
      <div class="setting-info">
        <div class="setting-label">I have saved both</div>
        <div class="setting-desc">Check this box to continue</div>
      </div>
      <input type="checkbox" id="rec-confirm">
    </div>
    <button class="btn primary" id="rec-continue" disabled style="opacity: 0.5; margin-top: 12px;">Continue to Cipher</button>
  `;
  showModal(html);

  const cb = document.getElementById('rec-confirm');
  const btn = document.getElementById('rec-continue');
  cb.addEventListener('change', () => {
    btn.disabled = !cb.checked;
    btn.style.opacity = cb.checked ? '1' : '0.5';
  });
  btn.addEventListener('click', () => {
    closeModal();
    enterApp();
  });
}

function copyText(text, btn) {
  navigator.clipboard.writeText(text).then(() => {
    if (btn) {
      const orig = btn.textContent;
      btn.textContent = 'Copied!';
      setTimeout(() => btn.textContent = orig, 1500);
    } else {
      toast('Copied to clipboard', 'success');
    }
  }).catch(() => toast('Copy failed', 'error'));
}

function downloadKey(username, key) {
  const content = `CIPHER RECOVERY KEY
====================
Username: ${username}
Generated: ${new Date().toISOString()}

RECOVERY KEY:
${key}

HOW TO USE:
Go to Cipher login page, click "Forgot password?", 
choose "Recovery file", and paste the key above.

DO NOT SHARE THIS FILE WITH ANYONE.
`;
  const blob = new Blob([content], { type: 'text/plain' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `cipher-recovery-${username}.key`;
  a.click();
  URL.revokeObjectURL(url);
  toast('Recovery file downloaded', 'success');
}

// ==========  FORGOT PASSWORD  ==========
function showRecoverModal() {
  const html = `
    <h3>🔓 Reset your <span class="accent">password</span></h3>
    <div class="tabs" style="margin-bottom: 20px;">
      <button class="tab active" onclick="switchRecoverTab(this, 'phrase')" type="button">Recovery phrase</button>
      <button class="tab" onclick="switchRecoverTab(this, 'file')" type="button">Recovery file</button>
    </div>
    <div id="recover-body"></div>
  `;
  showModal(html);
  renderRecoverForm('phrase');
}

function switchRecoverTab(btn, method) {
  btn.parentElement.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  renderRecoverForm(method);
}

function renderRecoverForm(method) {
  const body = document.getElementById('recover-body');
  if (method === 'phrase') {
    body.innerHTML = `
      <div class="field">
        <label>Username</label>
        <input type="text" id="rec-user" autocomplete="off">
      </div>
      <div class="field">
        <label>12-word recovery phrase</label>
        <textarea id="rec-phrase" rows="2" placeholder="word word word ..." style="font-family: 'JetBrains Mono', monospace;"></textarea>
      </div>
      <div class="field">
        <label>New password (6+ chars)</label>
        <input type="password" id="rec-newpass">
      </div>
      <button class="btn primary" onclick="doRecover('phrase')">Reset password</button>
      <p id="rec-msg" class="form-msg"></p>
    `;
  } else {
    body.innerHTML = `
      <div class="field">
        <label>Username</label>
        <input type="text" id="rec-user" autocomplete="off">
      </div>
      <div class="field">
        <label>Upload your recovery file</label>
        <input type="file" id="rec-file" accept=".key,.txt">
      </div>
      <div class="field">
        <label>Or paste the key</label>
        <textarea id="rec-key" rows="2" style="font-family: 'JetBrains Mono', monospace;" placeholder="Paste your recovery key here"></textarea>
      </div>
      <div class="field">
        <label>New password (6+ chars)</label>
        <input type="password" id="rec-newpass">
      </div>
      <button class="btn primary" onclick="doRecover('file')">Reset password</button>
      <p id="rec-msg" class="form-msg"></p>
    `;
    document.getElementById('rec-file').addEventListener('change', (e) => {
      const f = e.target.files[0];
      if (!f) return;
      const r = new FileReader();
      r.onload = (ev) => {
        const text = ev.target.result;
        const m = text.match(/RECOVERY KEY:\s*\n(.+)/i);
        document.getElementById('rec-key').value = m ? m[1].trim() : text.trim();
      };
      r.readAsText(f);
    });
  }
}

async function doRecover(method) {
  const msg = document.getElementById('rec-msg');
  msg.className = 'form-msg';
  const body = {
    username: document.getElementById('rec-user').value.trim(),
    new_password: document.getElementById('rec-newpass').value
  };
  if (method === 'phrase') {
    body.phrase = document.getElementById('rec-phrase').value.trim();
  } else {
    body.key = document.getElementById('rec-key').value.trim();
  }
  const res = await api(method === 'phrase' ? '/api/recover/phrase' : '/api/recover/key', {
    method: 'POST', body
  });
  if (!res.ok) {
    msg.textContent = res.data.error || 'Reset failed';
    return;
  }
  msg.className = 'form-msg success';
  msg.textContent = 'Password reset! You can sign in now.';
  setTimeout(closeModal, 2000);
}

// ==========  ToS MODAL (sections + a +5 bonus for actually reading)  ==========
async function showTosModal() {
  showModal(`
    <h3>📜 <span class="accent">Terms of Service</span></h3>
    <div class="tos-content" id="tos-content-body">Loading...</div>
  `, true);
  const res = await api('/api/tos');
  if (res.ok) {
    const sections = res.data.sections || {};
    const head = `<p class="tos-lead">Version ${esc(res.data.version || '')} — the short, human version first. Reading it to the end pays +5 💎, once.</p>`;
    const blocks = Object.entries(sections).map(([k, v]) =>
      `<div class="tos-section"><h4>${esc(v.title || k)}</h4><p>${esc(v.body || '')}</p></div>`).join('');
    const bodyEl = document.getElementById('tos-content-body');
    bodyEl.innerHTML = head + blocks + (blocks ? '' : `<p>${esc(res.data.tos || 'Failed to load ToS')}</p>`);
    if (!currentUser.tos_bonus_claimed) {
      bodyEl.insertAdjacentHTML('beforeend', `
        <div class="tos-bonus-row">
          <button class="btn primary" onclick="claimTosBonus()">I have read the terms — claim +5 💎</button>
        </div>`);
    }
  }
}

async function claimTosBonus() {
  const res = await api('/api/shards/tos_bonus', { method: 'POST' });
  if (!res.ok) return toast(res.data.error || 'Already claimed', 'warn');
  currentUser.tos_bonus_claimed = true;
  if (typeof res.data.balance === 'number') {
    currentUser.shards = res.data.balance;
    document.getElementById('shards-count').textContent = res.data.balance.toLocaleString();
  }
  toast('+5 Shards for reading the terms 📜', 'success');
  closeModal();
}

// ==========  CREDITS MODAL (dynamic color name — B3 fix)  ==========
function showCreditsModal() {
  const colorName = currentUser && currentUser.theme_color_name ? currentUser.theme_color_name : 'cyan';
  showModal(`
    <div class="credits-box">
      <div class="credits-logo">CIPHER</div>
      <div class="credits-version">v1.1.0</div>
      <div class="credits-line">— A solo project by —</div>
      <div class="credits-name">STEPUNDRIK</div>
      <div class="credits-roles">Backend · Frontend · Design<br>Database · Deployment</div>
      <div class="credits-line">Private messaging that doesn't stay forever.</div>
      <div class="credits-heart">Made with love and too much ${esc(colorName)}.</div>
      <div class="credits-copy">© 2025 · All rights reserved</div>
    </div>
  `);
}

// ==========  SESSION CHECK + ENTER APP  ==========
async function checkSession() {
  const res = await api('/api/me');
  if (res.data.poll_config) pollConfig = res.data.poll_config;
  if (res.data.encryption) serverEncryption = res.data.encryption;
  if (res.data.user) {
    currentUser = res.data.user;
    enterApp();
  }
  loadAnnouncements();
}

// ==========  BALANCES — LIVE REFRESH  ==========
let balanceTimer = null;

async function refreshBalances() {
  const res = await api('/api/me');
  if (!res.ok || !res.data.user) return;
  currentUser = res.data.user;
  const shardsEl = document.getElementById('shards-count');
  const coresEl = document.getElementById('cores-count');
  if (shardsEl) shardsEl.textContent = (currentUser.shards || 0).toLocaleString();
  if (coresEl) coresEl.textContent = (currentUser.cores || 0).toLocaleString();
}

function startBalancePolling() {
  if (balanceTimer) clearInterval(balanceTimer);
  balanceTimer = setInterval(() => {
    if (!document.hidden) refreshBalances();   // hidden tabs: one catch-up on focus
  }, 10000);
}

// Fresh numbers the moment the tab comes back, no waiting on the timer.
window.addEventListener('focus', () => { if (currentUser) refreshBalances(); });

function enterApp() {
  document.getElementById('auth-screen').classList.add('hidden');
  document.getElementById('app-screen').classList.remove('hidden');
  document.getElementById('my-username').textContent = currentUser.username;
  document.getElementById('empty-username').textContent = currentUser.username;

  // BUG 4: the me-card avatar goes through the one shared renderer
  paintAvatar(document.getElementById('me-avatar'), currentUser, 'lg');

  // Balances (Shards + Cores) — painted now, then kept fresh every 10s
  document.getElementById('shards-count').textContent = (currentUser.shards || 0).toLocaleString();
  document.getElementById('cores-count').textContent = (currentUser.cores || 0).toLocaleString();
  refreshBalances();
  startBalancePolling();

  updateTheme(currentUser.theme_color || '#00d9ff');

  if (currentUser.is_admin || currentUser.is_owner) {
    document.getElementById('admin-btn').classList.remove('hidden');
  }

  loadBadgeCatalog();
  initEncryption();
  refreshDailyButton();
  loadSpotlight();
  pollNotifications();

  loadConversations();
  startConvListPolling();
  loadAnnouncements();

  document.addEventListener('paste', handlePaste);
  const msgs = document.getElementById('messages');
  msgs.addEventListener('dragover', (e) => { e.preventDefault(); });
  msgs.addEventListener('drop', handleDrop);
  document.addEventListener('visibilitychange', () => {
    if (currentConv) restartPollingWithNewInterval();
    if (!document.hidden) refreshBalances();
  });
}

// ==========  LOGOUT  ==========
document.getElementById('logout-btn').addEventListener('click', async () => {
  await api('/api/logout', { method: 'POST' });
  location.reload();
});

// ==========  CONVERSATIONS LIST  ==========
async function loadConversations() {
  const res = await api('/api/conversations');
  if (!res.ok) return;
  const conversations = res.data.conversations || [];

  // Previews arrive as ciphertext; decrypt them once, then cache per message
  await Promise.all(conversations.map(async (c) => {
    if (!c.last_encrypted || !c.last_message) { c._preview = undefined; return; }
    const cached = decryptedPreview[c.last_message_id];
    if (cached !== undefined) { c._preview = cached; return; }
    const out = await HyperCrypt.decrypt(c.last_message);
    if (out.text == null) return;               // no key yet: show 🔒
    const text = out.text || (c.last_has_image ? '📷 Image' : '');
    decryptedPreview[c.last_message_id] = text;
    c._preview = text;
  }));

  const container = document.getElementById('conv-list');
  container.innerHTML = '';

  conversations.forEach(c => {
    const item = document.createElement('div');
    item.className = 'conv-item' + (c.id === currentConv ? ' active' : '');
    item.dataset.id = c.id;

    let icon, nameText, preview, anonClass = '';
    if (c.is_group) {
      icon = '💬';
      nameText = c.name || 'Group';
    } else {
      const partnerUser = (c.members || []).find(u => u.id !== currentUser.id);
      if (partnerUser && partnerUser.anonymous) {
        icon = '🕶';
        nameText = 'Anonymous';
        anonClass = ' anonymous';
      } else {
        icon = (partnerUser ? partnerUser.username : '?')[0].toUpperCase();
        nameText = partnerUser ? partnerUser.username : 'Unknown';
      }
    }

    if (c.last_encrypted) {
      preview = c._preview === undefined ? '🔒 Encrypted' : esc(c._preview);
    } else {
      preview = c.last_message ? esc(c.last_message) : (c.last_has_image ? '📷 Image' : 'No messages yet');
    }

    const isMineLast = c.last_message && c.last_sender_id === currentUser.id;
    const previewLine = isMineLast ? `You: ${preview}` : preview;

    // BUG 1: unread badge shows how many, not just a dot
    const unread = c.unread || 0;
    const unreadHtml = unread > 0
      ? `<div class="unread-badge">${unread > 99 ? '99+' : unread}</div>`
      : '';

    item.innerHTML = `
      <div class="avatar${anonClass}">${esc(icon)}</div>
      <div class="conv-meta">
        <div class="conv-name">${esc(nameText)}</div>
        <div class="conv-preview">${previewLine}</div>
      </div>
      ${unreadHtml}
    `;
    item.addEventListener('click', () => openConversation(c));
    container.appendChild(item);
  });

  updateEmptyConversations();
  refreshFriendBadge();
}

function startConvListPolling() {
  if (convListTimer) clearInterval(convListTimer);
  convListTimer = setInterval(loadConversations, 10000);
}

function openConversation(conv) {
  currentConv = conv.id;
  currentConvMeta = conv;

  document.getElementById('empty-state').classList.add('hidden');
  document.getElementById('chat-view').classList.remove('hidden');
  document.getElementById('msg-input').focus();

  document.querySelectorAll('.conv-item').forEach(el => {
    el.classList.toggle('active', el.dataset.id === conv.id);
  });

  const nameEl = document.getElementById('chat-title');
  const avEl = document.getElementById('chat-avatar');
  const subEl = document.getElementById('chat-subtitle');
  let avatarUser = null;

  if (conv.is_group) {
    nameEl.textContent = '💬 ' + (conv.name || 'Group');
    subEl.textContent = `${(conv.members || []).length} members`;
    avEl.className = 'avatar md';
    avEl.textContent = (conv.name || 'G')[0].toUpperCase();
    avEl.style.background = 'linear-gradient(135deg, #6366f1, var(--bg-5))';
    avEl.onclick = () => showGroupInfo();
  } else {
    const partnerUser = (conv.members || []).find(u => u.id !== currentUser.id);
    // BUG 1: anonymity holds in the chat header too
    if (partnerUser && partnerUser.anonymous) {
      nameEl.textContent = '🕶 Anonymous';
      subEl.textContent = '';
      avEl.className = 'avatar md anonymous';
      avEl.textContent = '🕶';
      avEl.onclick = null;
    } else {
      nameEl.textContent = partnerUser ? partnerUser.username : 'Chat';
      subEl.textContent = '';
      avatarUser = partnerUser;
    }
  }
  if (avatarUser) {
    paintAvatar(avEl, avatarUser, 'md');
    avEl.style.cursor = 'pointer';
    avEl.onclick = () => showUserProfile(avatarUser.username);
  }

  document.getElementById('group-info-btn').classList.toggle('hidden', !conv.is_group);
  const muteBtn = document.getElementById('mute-btn');
  muteBtn.style.color = conv.muted ? 'var(--warn)' : '';

  // Make sure we hold this conversation's key before the first poll
  HyperCrypt.ensureConversationKey(api, conv.id, cryptoMembers(), masterPublicKey())
    .catch(() => {})
    .finally(() => { msgListHash = ''; loadMessages(); restartPollingWithNewInterval(); });
}

// ==========  MESSAGES  ==========
async function loadMessages() {
  if (!currentConv) return;
  const cid = currentConv;
  // Incremental polling: the server only sends messages newer than this
  const since = lastMessageStamp[cid];
  const res = await api(`/api/messages/${cid}` + (since ? `?since=${encodeURIComponent(since)}` : ''));
  if (!res.ok) return;
  if (cid !== currentConv) return;   // switched away while it was in flight

  let msgs = res.data.messages || [];
  if (res.data.incremental) {
    const existing = messageCache[cid] || [];
    const seen = new Set(existing.map(m => m.id));
    msgs = existing.concat(msgs.filter(m => !seen.has(m.id)));
  }

  // Decrypt anything new. Already-decrypted messages are cached so a 2s
  // poll does not re-run AES on the whole history.
  let decryptedNew = false;
  for (const m of msgs) {
    if (m._text !== undefined) continue;
    if (!m.encrypted) { m._text = m.content; continue; }
    const out = await HyperCrypt.decrypt(m.content);
    m._text = out.text != null ? out.text : null;
    if (out.text == null && out.reason === 'no_key') {
      // A conversation key we have not fetched yet (e.g. a group we just joined)
      await HyperCrypt.ensureConversationKey(api, cid, cryptoMembers(), masterPublicKey());
      const retry = await HyperCrypt.decrypt(m.content);
      if (retry.text != null) m._text = retry.text;
    }
    decryptedNew = true;
  }

  messageCache[cid] = msgs;
  if (msgs.length) lastMessageStamp[cid] = msgs[msgs.length - 1].created_at;

  const box = document.getElementById('messages');
  const wasAtBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 100;
  const h = JSON.stringify(msgs.map(m => [m.id, m.created_at, m.reactions, m.read_by, m.is_anonymous]));
  if (h === msgListHash && !decryptedNew) {
    idleCycles++;
  } else {
    msgListHash = h;
    idleCycles = 0;
    renderMessages(msgs, box, wasAtBottom);
  }

  const warn = document.getElementById('expiry-warning');
  const warnText = document.getElementById('expiry-warning-text');
  const soon = res.data.expiring_soon || 0;
  if (soon > 0 && currentUser.notify_before_delete) {
    warn.classList.remove('hidden');
    warnText.textContent = `${soon} message${soon === 1 ? '' : 's'} will be deleted within 48 hours.`;
  } else {
    warn.classList.add('hidden');
  }
}

function renderMessages(msgs, box, keepScroll) {
  const wasAtBottom = !keepScroll || isScrolledToBottom(box);
  box.innerHTML = '';

  let lastSender = null;
  let lastTime = 0;

  msgs.forEach((m, i) => {
    const when = new Date(m.created_at);
    const sender = m.users || {};

    // Insert a date divider when the day changes
    const prev = i > 0 ? new Date(msgs[i - 1].created_at) : null;
    if (!prev || prev.toDateString() !== when.toDateString()) {
      const div = document.createElement('div');
      div.className = 'date-divider';
      div.textContent = formatDateDivider(when);
      box.appendChild(div);
    }

    const mine = m.sender_id === currentUser.id;
    const isAnon = m.is_anonymous === true;

    // Group by "sender identity" — anonymous messages never group
    const senderKey = isAnon ? '_anon_' + (m.id.slice(0, 8)) : (m.sender_id || '_unknown_');
    const sameSender = senderKey === lastSender;
    const closeInTime = (when.getTime() - lastTime) < 5 * 60 * 1000;
    const grouped = sameSender && closeInTime && !isAnon;

    let group;
    if (grouped) {
      group = box.lastElementChild;
    } else {
      group = document.createElement('div');
      group.className = 'msg-group ' + (mine ? 'mine' : 'theirs');
      if (!mine) {
        let senderName, color, fontClass = '', avHtml, badges = '';
        if (isAnon) {
          senderName = 'Anonymous';
          color = 'var(--text-mute)';
          avHtml = `<div class="avatar xs anonymous" title="Anonymous">🕶</div>`;
        } else {
          senderName = sender.username || 'Unknown';
          color = sender.nickname_color || '#00d9ff';
          fontClass = sender.name_font ? ' font-' + sender.name_font : '';
          // BUG 4: effects ride on every avatar, not just the sidebar one
          avHtml = avatarHtml(sender, 'xs', true);
          badges = badgesHtml(sender.active_badges, { max: 2 });
        }
        const anonTag = isAnon ? ' <span class="anonymous-tag">anon</span>' : '';
        const senderClass = isAnon ? 'msg-sender anonymous' : 'msg-sender';
        const clickName = (!isAnon && sender.username)
          ? ` onclick="event.stopPropagation(); showUserProfile('${esc(sender.username)}')"`
          : '';
        group.innerHTML = `<div class="msg-sender-row">${avHtml}` +
          `<div><div class="${senderClass}${fontClass}" style="color: ${esc(color)}"${clickName}>${esc(senderName)}${anonTag}</div>` +
          `${badges}</div></div>`;
      }
      box.appendChild(group);
    }

    // Message bubble with optional animation class
    const bubble = document.createElement('div');
    let bubbleClass = 'msg';
    if (mine && currentUser.active_message_animation) {
      bubbleClass += ' anim-' + currentUser.active_message_animation;
    }
    bubble.className = bubbleClass;

    if (mine && currentUser.active_bubble_color) {
      bubble.style.background = currentUser.active_bubble_color;
      bubble.style.color = getContrastColor(currentUser.active_bubble_color);
      bubble.style.boxShadow = '0 2px 8px ' + currentUser.active_bubble_color + '55';
    }

    // Encrypted messages carry their plaintext here once decrypted
    const text = (m._text !== undefined ? m._text : m.content);
    let inner = '';
    if (m.image_url) {
      if (m.encrypted && !m._blob) {
        inner += `<img class="msg-img pending" data-msg="${esc(m.id)}" alt="image">`;
      } else {
        const src = m._blob || m.image_url;
        inner += `<img src="${esc(src)}" class="msg-img" alt="image" onclick="viewImage('${esc(src)}')">`;
      }
    }
    if (text) {
      const cls = m.image_url ? 'msg-text' : '';
      inner += `<div class="${cls}">${esc(text)}</div>`;
    } else if (!m.image_url && m.encrypted) {
      inner += `<div class="msg-locked">🔒 encrypted</div>`;
    }
    bubble.innerHTML = inner;
    group.appendChild(bubble);

    const isLastOfGroup = (i === msgs.length - 1) || (msgs[i + 1] && (
      (msgs[i + 1].is_anonymous ? '_anon_' + msgs[i + 1].id.slice(0, 8) : (msgs[i + 1].sender_id || '_unknown_')) !== senderKey ||
      (new Date(msgs[i + 1].created_at) - when) >= 5 * 60 * 1000 ||
      isAnon
    ));

    if (isLastOfGroup) {
      const meta = document.createElement('div');
      meta.className = 'msg-meta';
      let metaHtml = `<span class="msg-time">${formatTime(m.created_at)}</span>`;
      if (m.encrypted) metaHtml += ` <span class="msg-lock" title="End-to-end encrypted">🔒</span>`;
      metaHtml += ` <button class="react-btn" type="button" onclick="showEmojiPicker(event, '${m.id}')">＋</button>`;
      if (mine && m.read_by && m.read_by.filter(u => u !== currentUser.username).length > 0) {
        metaHtml += ` <span class="msg-seen" title="Seen">✓✓</span>`;
      }
      meta.innerHTML = metaHtml;
      group.appendChild(meta);
    }

    if (m.reactions && m.reactions.length > 0) {
      const counts = {};
      m.reactions.forEach(r => {
        counts[r.emoji] = counts[r.emoji] || { count: 0, mine: false };
        counts[r.emoji].count++;
        if (r.user_id === currentUser.id) counts[r.emoji].mine = true;
      });
      Object.entries(counts).forEach(([emoji, info]) => {
        const chip = document.createElement('button');
        chip.className = 'reaction' + (info.mine ? ' mine' : '');
        chip.type = 'button';
        chip.innerHTML = `${esc(emoji)} <span>${info.count}</span>`;
        chip.addEventListener('click', () => {
          api(`/api/messages/${currentConv}/react`, {
            method: 'POST',
            body: { message_id: m.id, emoji }
          }).then(() => { msgListHash = ''; loadMessages(); });
        });
        group.appendChild(chip);
      });
    }

    lastSender = senderKey;
    lastTime = when.getTime();
  });

  if (!msgs.length) {
    box.innerHTML = '<div class="empty-msgs">No messages yet. Say something.</div>';
  } else if (wasAtBottom) {
    requestAnimationFrame(() => { box.scrollTop = box.scrollHeight; });
  }
  resolveEncryptedImages(box);
}

/* Fill in attachments that arrived as ciphertext. The file lives on a private
   bucket; the browser fetches it through /api/file/<path>?cid=..., which the
   server only serves to a member of that conversation. */
async function resolveEncryptedImages(box) {
  const pending = box.querySelectorAll('img.pending[data-msg]');
  const list = messageCache[currentConv] || [];
  for (const img of pending) {
    const msg = list.find(m => m.id === img.dataset.msg);
    if (!msg || !msg.image_url) continue;
    if (msg._blob) {
      img.classList.remove('pending');
      img.src = msg._blob;
      img.onclick = () => viewImage(msg._blob);
      continue;
    }
    try {
      const path = msg.image_url.replace(/^\/(api\/file\/)?/, '');
      const url = `/api/file/${encodeURI(path)}?cid=${encodeURIComponent(currentConv)}`;
      msg._blob = await HyperCrypt.decryptImageBlob(url);
      img.classList.remove('pending');
      img.src = msg._blob;
      img.onclick = () => viewImage(msg._blob);
    } catch (err) {
      img.classList.remove('pending');
      img.alt = '🔒 image could not be decrypted';
    }
  }
}

function getContrastColor(hex) {
  if (!hex || !hex.startsWith('#')) return '#fff';
  try {
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    const brightness = (r * 299 + g * 587 + b * 114) / 1000;
    return brightness > 160 ? '#001820' : '#ffffff';
  } catch { return '#fff'; }
}

async function toggleReaction(msgId, emoji) {
  await api(`/api/messages/${msgId}/react`, { method: 'POST', body: { emoji } });
  msgListHash = '';
  loadMessages();
}

function viewImage(url) {
  document.getElementById('img-viewer-img').src = url;
  document.getElementById('img-viewer').classList.remove('hidden');
}

// ==========  EMOJI PICKER  ==========
function showEmojiPicker(evt, msgId) {
  evt.stopPropagation();
  document.querySelectorAll('.emoji-picker').forEach(x => x.remove());

  const emojis = ['👍','❤️','😂','😮','😢','🔥','💯','👀'];
  const picker = document.createElement('div');
  picker.className = 'emoji-picker';
  emojis.forEach(em => {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = em;
    b.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleReaction(msgId, em);
      picker.remove();
    });
    picker.appendChild(b);
  });
  document.body.appendChild(picker);

  const rect = evt.target.getBoundingClientRect();
  const pw = picker.offsetWidth;
  const ph = picker.offsetHeight;
  const vw = window.innerWidth;
  const vh = window.innerHeight;

  let top = rect.top - ph - 8;
  let left = rect.left;
  if (top < 10) top = rect.bottom + 8;
  if (left + pw > vw - 10) left = vw - pw - 10;
  if (left < 10) left = 10;
  if (top + ph > vh - 10) top = vh - ph - 10;

  picker.style.top = top + 'px';
  picker.style.left = left + 'px';

  setTimeout(() => {
    const closer = () => { picker.remove(); document.removeEventListener('click', closer); };
    document.addEventListener('click', closer);
  }, 50);
}

// ==========  SEND MESSAGE  ==========
document.getElementById('msg-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = document.getElementById('msg-input');
  const content = input.value.trim();
  if (!content && !pendingImage) return;
  if (!currentConv) return;

  // HyperCrypt: the plaintext never leaves this device unencrypted
  let body;
  if (encryptionReady) {
    try {
      await HyperCrypt.ensureConversationKey(api, currentConv, cryptoMembers(), masterPublicKey());
      body = { content: await HyperCrypt.encrypt(api, currentConv, cryptoMembers(), content) };
      if (pendingImage) body.image_data = await encryptImageForSend(pendingImage);
    } catch (err) {
      toast('Could not encrypt this message: ' + (err && err.message || err), 'error', 6000);
      input.value = content;
      return;
    }
  } else {
    body = { content };
    if (pendingImage) body.image_data = pendingImage;
  }

  input.value = '';
  const imgToSend = pendingImage;
  clearImagePreview();

  const res = await api(`/api/messages/${currentConv}`, { method: 'POST', body });

  if (res.data.blocked) {
    showWarningModal({ body: res.data.error });
    return;
  }
  if (!res.ok) {
    toast(res.data.error || 'Failed to send', 'error');
    input.value = content;
    if (imgToSend) pendingImage = imgToSend;
    return;
  }

  if (res.data.warning) {
    // Big centered warning with the sarcastic quip + the Sorry button
    showWarningModal({ body: res.data.warning });
  }
  if (res.data.throttle_delay) {
    toast(`You are throttled. Messages send with a delay.`, 'warn', 3000);
  }

  msgListHash = '';
  loadMessages();
});

// ==========  TYPING  ==========
document.getElementById('msg-input').addEventListener('input', () => {
  if (!currentConv) return;
  const now = Date.now();
  if (now - typingSentAt > 3000) {
    typingSentAt = now;
    api(`/api/typing/${currentConv}`, { method: 'POST' });
  }
});

async function checkTyping() {
  if (!currentConv) return;
  const res = await api(`/api/typing/${currentConv}`);
  const ind = document.getElementById('typing-indicator');
  const text = document.getElementById('typing-text');
  const typing = res.data.typing || [];
  if (typing.length === 0) {
    ind.classList.add('hidden');
    return;
  }
  ind.classList.remove('hidden');
  if (typing.length === 1) text.textContent = `${typing[0]} is typing…`;
  else text.textContent = `${typing.join(', ')} are typing…`;
}

// ==========  IMAGE UPLOAD (file, paste, drop)  ==========
document.getElementById('img-input').addEventListener('change', async (e) => {
  const f = e.target.files[0];
  if (f) await handleImageFile(f);
  e.target.value = '';
});

async function handleImageFile(file) {
  if (!file.type.startsWith('image/')) {
    toast('Only images are allowed', 'error');
    return;
  }
  toast('Processing image...', 'info', 1500);
  try {
    const compressed = await compressImage(file);
    pendingImage = compressed;
    showImagePreview(compressed);
  } catch (err) {
    toast('Image processing failed', 'error');
  }
}

function compressImage(file, maxDim = 1200, quality = 0.7) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = (e) => {
      const img = new Image();
      img.onload = () => {
        let w = img.width, h = img.height;
        if (w > maxDim || h > maxDim) {
          if (w > h) { h = h * (maxDim / w); w = maxDim; }
          else { w = w * (maxDim / h); h = maxDim; }
        }
        const canvas = document.createElement('canvas');
        canvas.width = w; canvas.height = h;
        canvas.getContext('2d').drawImage(img, 0, 0, w, h);
        resolve(canvas.toDataURL('image/jpeg', quality));
      };
      img.onerror = () => reject(new Error('Bad image'));
      img.src = e.target.result;
    };
    reader.onerror = () => reject(new Error('Read failed'));
    reader.readAsDataURL(file);
  });
}

function showImagePreview(dataUrl) {
  const box = document.getElementById('image-preview');
  box.innerHTML = `<img src="${dataUrl}"><button class="btn-mini ghost" type="button" onclick="clearImagePreview()">Remove</button>`;
  box.classList.remove('hidden');
}

function clearImagePreview() {
  pendingImage = null;
  const box = document.getElementById('image-preview');
  box.innerHTML = '';
  box.classList.add('hidden');
}

function handlePaste(e) {
  if (!currentConv) return;
  const items = (e.clipboardData || e.originalEvent?.clipboardData)?.items;
  if (!items) return;
  for (const item of items) {
    if (item.type.startsWith('image/')) {
      e.preventDefault();
      const blob = item.getAsFile();
      if (blob) handleImageFile(blob);
      return;
    }
  }
}

function handleDrop(e) {
  e.preventDefault();
  if (!currentConv) return;
  const files = e.dataTransfer?.files;
  if (files && files[0]) handleImageFile(files[0]);
}

// ==========  CHAT ACTIONS  ==========
async function toggleMute() {
  if (!currentConv) return;
  const newState = !currentConvMeta.muted;
  await api(`/api/conversations/${currentConv}/mute`, { method: 'POST', body: { muted: newState } });
  currentConvMeta.muted = newState;
  document.getElementById('mute-btn').style.color = newState ? 'var(--warn)' : '';
  toast(newState ? 'Muted' : 'Unmuted', 'success');
  convListHash = '';
  loadConversations();
}

function leaveCurrent() {
  if (!currentConv) return;
  showModal(`
    <h3>Leave <span class="accent">this conversation?</span></h3>
    <p>You will no longer see messages from this chat. If everyone leaves, it will be permanently deleted.</p>
    <button class="btn danger" onclick="confirmLeave()">Yes, leave</button>
    <button class="btn secondary" style="margin-top: 8px;" onclick="closeModal()">Cancel</button>
  `);
}

async function confirmLeave() {
  const cid = currentConv;
  await api(`/api/conversations/${cid}/leave`, { method: 'POST' });
  currentConv = null;
  currentConvMeta = null;
  document.getElementById('chat-view').classList.add('hidden');
  document.getElementById('empty-state').classList.remove('hidden');
  closeModal();
  convListHash = '';
  loadConversations();
  toast('Left conversation', 'success');
}

function exportCurrent() {
  if (!currentConv) return;
  window.location.href = `/api/conversations/${currentConv}/export`;
}

async function extendAllMessages() {
  if (!currentConv) return;
  const res = await api(`/api/conversations/${currentConv}/extend_all`, { method: 'POST' });
  if (res.ok) {
    toast('Messages extended by 30 days', 'success');
    document.getElementById('expiry-warning').classList.add('hidden');
    msgListHash = '';
    loadMessages();
  } else {
    toast('Failed to extend', 'error');
  }
}

function dismissExpiryWarning() {
  document.getElementById('expiry-warning').classList.add('hidden');
}

// ==========  GROUP INFO  ==========
function showGroupInfo() {
  if (!currentConvMeta || !currentConvMeta.is_group) return;
  const c = currentConvMeta;
  const iAmAdmin = c.i_am_group_admin;

  const memberRows = c.members.map(m => `
    <tr>
      <td>
        <span style="color: ${esc(m.nickname_color)}; font-weight: 500; cursor: pointer;" onclick="showUserProfile('${esc(m.username)}')">${esc(m.username)}</span>
        ${m.is_group_admin ? '<span class="badge admin">Admin</span>' : ''}
      </td>
      <td style="text-align: right;">
        ${iAmAdmin && m.id !== currentUser.id ? `<button class="btn-mini danger" onclick="removeGroupMember('${m.id}', '${esc(m.username)}')">Remove</button>` : ''}
      </td>
    </tr>
  `).join('');

  showModal(`
    <h3>Group: <span class="accent">${esc(c.name || 'Unnamed')}</span></h3>

    ${iAmAdmin ? `
      <h4>Rename group</h4>
      <div style="display: flex; gap: 8px;">
        <input type="text" id="grp-newname" value="${esc(c.name || '')}" style="flex: 1;">
        <button class="btn-mini" onclick="renameGroup()">Save</button>
      </div>

      <h4>Add member</h4>
      <div style="display: flex; gap: 8px;">
        <input type="text" id="grp-adduser" placeholder="Username to add" style="flex: 1;">
        <button class="btn-mini" onclick="addGroupMember()">Add</button>
      </div>
    ` : ''}

    <h4>Members (${c.members.length})</h4>
    <table><tbody>${memberRows}</tbody></table>
  `);
}

async function renameGroup() {
  const name = document.getElementById('grp-newname').value.trim();
  if (!name) return toast('Name required', 'error');
  const res = await api(`/api/conversations/${currentConv}/rename`, { method: 'POST', body: { name } });
  if (res.ok) {
    toast('Group renamed', 'success');
    document.getElementById('chat-title').textContent = name;
    currentConvMeta.name = name;
    convListHash = '';
    loadConversations();
    closeModal();
  } else toast(res.data.error, 'error');
}

async function addGroupMember() {
  const username = document.getElementById('grp-adduser').value.trim();
  if (!username) return;
  const res = await api(`/api/conversations/${currentConv}/add_member`, { method: 'POST', body: { username } });
  if (res.ok) {
    toast('Member added', 'success');
    const cRes = await api('/api/conversations');
    const updated = (cRes.data.conversations || []).find(c => c.id === currentConv);
    if (updated) { currentConvMeta = updated; showGroupInfo(); }
  } else toast(res.data.error, 'error');
}

async function removeGroupMember(uid, username) {
  showModal(`
    <h3>Remove <span class="accent">${esc(username)}</span>?</h3>
    <p>They will no longer see messages in this group.</p>
    <button class="btn danger" onclick="confirmRemoveMember('${uid}')">Remove ${esc(username)}</button>
    <button class="btn secondary" style="margin-top: 8px;" onclick="showGroupInfo()">Cancel</button>
  `);
}

async function confirmRemoveMember(uid) {
  const res = await api(`/api/conversations/${currentConv}/remove_member`, { method: 'POST', body: { user_id: uid } });
  if (res.ok) {
    toast('Member removed', 'success');
    const cRes = await api('/api/conversations');
    const updated = (cRes.data.conversations || []).find(c => c.id === currentConv);
    if (updated) { currentConvMeta = updated; showGroupInfo(); }
  } else toast(res.data.error, 'error');
}

// ==========  NEW CHAT / GROUP MODALS  ==========
document.getElementById('new-chat-btn').addEventListener('click', () => {
  showModal(`
    <h3>Start a <span class="accent">new chat</span></h3>
    <p>Enter the username of the person you want to message.</p>
    <div class="field"><input type="text" id="nc-user" placeholder="username" autofocus></div>
    <button class="btn primary" onclick="createDM()">Start chat</button>
    <p id="nc-msg" class="form-msg"></p>
  `);
});

async function createDM() {
  const username = document.getElementById('nc-user').value.trim();
  if (!username) return;
  const res = await api('/api/conversations/new_dm', { method: 'POST', body: { username } });
  if (!res.ok) {
    document.getElementById('nc-msg').textContent = res.data.error || 'Failed';
    return;
  }
  closeModal();
  await loadConversations();
  const cRes = await api('/api/conversations');
  const conv = (cRes.data.conversations || []).find(c => c.id === res.data.conversation_id);
  if (conv) openConversation(conv);
}

document.getElementById('new-group-btn').addEventListener('click', () => {
  showModal(`
    <h3>New <span class="accent">group chat</span></h3>
    <div class="field">
      <label>Group name</label>
      <input type="text" id="ng-name" placeholder="e.g. Friends" autofocus>
    </div>
    <div class="field">
      <label>Members (usernames, comma-separated)</label>
      <input type="text" id="ng-users" placeholder="alice, bob, charlie">
    </div>
    <button class="btn primary" onclick="createGroup()">Create group</button>
    <p id="ng-msg" class="form-msg"></p>
  `);
});

async function createGroup() {
  const name = document.getElementById('ng-name').value.trim();
  const usernames = document.getElementById('ng-users').value.split(',').map(s => s.trim()).filter(Boolean);
  if (!name) return document.getElementById('ng-msg').textContent = 'Name required';
  if (!usernames.length) return document.getElementById('ng-msg').textContent = 'At least one member required';
  const res = await api('/api/conversations/new_group', { method: 'POST', body: { name, usernames } });
  if (!res.ok) {
    document.getElementById('ng-msg').textContent = res.data.error || 'Failed';
    return;
  }
  closeModal();
  await loadConversations();
  const cRes = await api('/api/conversations');
  const conv = (cRes.data.conversations || []).find(c => c.id === res.data.conversation_id);
  if (conv) openConversation(conv);
}

// ==========  SEARCH  ==========
document.getElementById('search-btn').addEventListener('click', () => {
  showModal(`
    <h3>🔍 <span class="accent">Search messages</span></h3>
    <div class="field"><input type="text" id="search-q" placeholder="Type to search..." autofocus></div>
    <div id="search-results"></div>
  `);
  let debounce;
  document.getElementById('search-q').addEventListener('input', (e) => {
    clearTimeout(debounce);
    const q = e.target.value.trim();
    debounce = setTimeout(async () => {
      const box = document.getElementById('search-results');
      if (q.length < 2) { box.innerHTML = ''; return; }
      const res = await api('/api/search?q=' + encodeURIComponent(q));
      const results = res.data.results || [];
      if (results.length === 0) {
        box.innerHTML = '<p style="text-align:center; color:var(--text-mute); padding:20px;">No results</p>';
        return;
      }
      box.innerHTML = results.map(r => {
        const senderName = r.is_anonymous ? 'Anonymous' : (r.users?.username || 'unknown');
        return `
          <div class="search-result" onclick='openFromSearch("${r.conversation_id}")'>
            <div class="search-result-meta">
              <span class="search-result-user">${esc(senderName)}</span>
              <span class="search-result-time">${new Date(r.created_at).toLocaleString()}</span>
            </div>
            <div class="search-result-content">${esc((r.content || '').slice(0, 150))}</div>
          </div>
        `;
      }).join('');
    }, 300);
  });
});

async function openFromSearch(cid) {
  closeModal();
  const cRes = await api('/api/conversations');
  const conv = (cRes.data.conversations || []).find(c => c.id === cid);
  if (conv) openConversation(conv);
}

// ==========  INVITES  ==========
// The sidebar button is gone (invites were superseded by promo codes), but
// the modal and the /api/invites backend stay intact for existing links.
// eslint-disable-next-line no-unused-vars

async function showInvitesModal() {
  const res = await api('/api/invites');
  const invites = res.data.invites || [];
  const rows = invites.map(i => `
    <tr>
      <td><code>${esc(i.code)}</code></td>
      <td>${i.uses_count}${i.max_uses ? '/' + i.max_uses : ''}</td>
      <td>${i.revoked ? '<span class="badge critical">Revoked</span>' : '<span style="color:var(--success);">Active</span>'}</td>
      <td>
        <button class="btn-mini ghost" onclick="copyInvite('${esc(i.code)}')">Copy</button>
        ${!i.revoked ? `<button class="btn-mini danger" onclick="revokeInvite('${i.id}')">Revoke</button>` : ''}
      </td>
    </tr>
  `).join('');

  showModal(`
    <h3>🎟️ <span class="accent">Invite links</span></h3>
    <div style="display: flex; gap: 8px; margin-bottom: 16px; align-items: center;">
      <input type="number" id="inv-uses" placeholder="Max uses (blank = ∞)" style="flex: 1; margin: 0;">
      <input type="number" id="inv-hours" placeholder="Expires in hours" style="flex: 1; margin: 0;">
      <button class="btn primary" style="width: auto; padding: 11px 18px;" onclick="createInvite()">Create</button>
    </div>
    <table>
      <thead><tr><th>Code</th><th>Uses</th><th>Status</th><th></th></tr></thead>
      <tbody>${rows || '<tr class="empty-row"><td colspan="4">No invites yet</td></tr>'}</tbody>
    </table>
  `);
}

async function createInvite() {
  const max_uses = parseInt(document.getElementById('inv-uses').value) || null;
  const expires_hours = parseInt(document.getElementById('inv-hours').value) || null;
  const res = await api('/api/invites', { method: 'POST', body: { max_uses, expires_hours } });
  if (!res.ok) return toast(res.data.error || 'Failed', 'error');
  toast('Invite created', 'success');
  showInvitesModal();
}

function copyInvite(code) {
  const url = window.location.origin + '/?invite=' + code;
  copyText(url);
}

async function revokeInvite(id) {
  await api(`/api/invites/${id}/revoke`, { method: 'POST' });
  toast('Invite revoked', 'success');
  showInvitesModal();
}
// ==========  SETTINGS MODAL  ==========
document.getElementById('settings-btn').addEventListener('click', showSettingsModal);

function showSettingsModal() {
  const u = currentUser;
  showModal(`
    <h3>⚙️ Your <span class="accent">settings</span></h3>

    <h4>Appearance</h4>
    <div class="color-row">
      <label>Nickname color</label>
      <input type="color" id="s-nick" value="${esc(u.nickname_color)}">
    </div>
    <div class="color-row">
      <label>Theme accent color</label>
      <input type="color" id="s-theme" value="${esc(u.theme_color)}">
    </div>

    <h4>Privacy</h4>
    <div class="setting-row">
      <div class="setting-info">
        <div class="setting-label">Anonymous mode</div>
        <div class="setting-desc">Hide your name on new messages (recipients see "Anonymous")</div>
      </div>
      <input type="checkbox" id="s-anon" ${u.anonymous_mode ? 'checked' : ''}>
    </div>
    <div class="setting-row">
      <div class="setting-info">
        <div class="setting-label">Keep all my messages forever</div>
        <div class="setting-desc">Disable auto-delete on your messages</div>
      </div>
      <input type="checkbox" id="s-keep" ${u.keep_all_forever ? 'checked' : ''}>
    </div>
    <div class="setting-row">
      <div class="setting-info">
        <div class="setting-label">Delete warning notifications</div>
        <div class="setting-desc">Show a banner 48h before messages expire</div>
      </div>
      <input type="checkbox" id="s-notify" ${u.notify_before_delete ? 'checked' : ''}>
    </div>
    <div class="setting-row">
      <div class="setting-info">
        <div class="setting-label">Hide me from leaderboard</div>
        <div class="setting-desc">You won't appear in public rankings</div>
      </div>
      <input type="checkbox" id="s-lb" ${u.leaderboard_opt_out ? 'checked' : ''}>
    </div>

    <div class="setting-row">
      <div class="setting-info">
        <div class="setting-label">Streamer mode</div>
        <div class="setting-desc">Blur names, avatars and previews so nobody shoulder-surfs your chats</div>
      </div>
      <input type="checkbox" id="s-streamer" ${u.streamer_mode && u.streamer_mode.enabled ? 'checked' : ''}>
    </div>
    <div class="setting-row">
      <div class="setting-info">
        <div class="setting-label">Friend requests</div>
        <div class="setting-desc">Who is allowed to add you</div>
      </div>
      <select id="s-friends" class="mini-select">
        <option value="open" ${u.friend_privacy === 'open' ? 'selected' : ''}>Everyone adds me instantly</option>
        <option value="approval" ${u.friend_privacy === 'approval' ? 'selected' : ''}>I approve each request</option>
        <option value="closed" ${u.friend_privacy === 'closed' ? 'selected' : ''}>Nobody can add me</option>
      </select>
    </div>

    <h4>Profile</h4>
    <div class="setting-row">
      <div class="setting-info">
        <div class="setting-label">Username</div>
        <div class="setting-desc">${esc(u.username)} — you can change it a few times an hour</div>
      </div>
      <button class="btn-mini" type="button" onclick="showChangeUsernameModal()">Change</button>
    </div>
    <div class="field">
      <label>Bio</label>
      <textarea id="s-bio" rows="2" maxlength="160" placeholder="Tell people about you (max 160 chars)">${esc(u.bio || '')}</textarea>
    </div>

    <button class="btn primary" style="margin-top: 12px;" onclick="saveSettings()">Save changes</button>

    <h4>🔐 Encryption</h4>
    <div class="setting-row">
      <div class="setting-info">
        <div class="setting-label">HyperCrypt identity</div>
        <div class="setting-desc" id="s-enc-status">${encryptionReady ? 'Active on this device' : 'Setting up…'}</div>
      </div>
      <button class="btn-mini" type="button" onclick="showEncryptionModal()">Details</button>
    </div>

    <h4>Security</h4>
    <button class="btn secondary" onclick="showChangePasswordModal()" style="margin-bottom: 8px;">Change password</button>
    ${u.totp_enabled
      ? `<button class="btn secondary" onclick="showDisable2FAModal()">Disable 2FA</button>`
      : `<button class="btn secondary" onclick="showEnable2FAModal()">Enable 2FA (Authenticator App)</button>`
    }

    <h4>Legal</h4>
    <button class="btn secondary" onclick="showTosModal()">Read Terms of Service</button>

    ${!u.is_owner ? `
      <h4>Danger zone</h4>
      <button class="btn danger" onclick="showDeleteAccountModal()">Delete my account</button>
    ` : `
      <h4>Owner note</h4>
      <p style="color: var(--text-mute); font-size: 12px;">The owner account cannot be deleted from within the app.</p>
    `}
  `, false, true);
}

async function saveSettings() {
  const body = {
    nickname_color: document.getElementById('s-nick').value,
    theme_color: document.getElementById('s-theme').value,
    anonymous_mode: document.getElementById('s-anon').checked,
    keep_all_forever: document.getElementById('s-keep').checked,
    notify_before_delete: document.getElementById('s-notify').checked,
    leaderboard_opt_out: document.getElementById('s-lb').checked,
    friend_privacy: document.getElementById('s-friends').value,
    bio: document.getElementById('s-bio').value
  };
  const res = await api('/api/profile', { method: 'POST', body });
  if (!res.ok) return toast(res.data.error || 'Save failed', 'error');

  // Streamer mode lives on its own endpoint (JSONB toggles + global forces)
  const streamerOn = document.getElementById('s-streamer').checked;
  const sr = await api('/api/settings/streamer', { method: 'POST', body: { enabled: streamerOn } });
  if (sr.ok) currentUser.streamer_mode = sr.data.streamer_mode || currentUser.streamer_mode;

  Object.assign(currentUser, body);
  currentUser.theme_color_name = getColorNameClient(body.theme_color);
  updateTheme(body.theme_color);
  paintAvatar(document.getElementById('me-avatar'), currentUser, 'lg');
  toast('Settings saved', 'success');
  closeModal();
  convListHash = '';
  loadConversations();
}

// Local color name mapping (matches backend)
function getColorNameClient(hex) {
  const colors = {
    'cyan': [0, 217, 255], 'red': [239, 68, 68], 'orange': [245, 158, 11],
    'yellow': [234, 179, 8], 'green': [34, 197, 94], 'blue': [59, 130, 246],
    'purple': [139, 92, 246], 'pink': [236, 72, 153], 'rose': [244, 63, 94],
    'white': [255, 255, 255], 'black': [0, 0, 0], 'gray': [107, 114, 128],
    'indigo': [99, 102, 241], 'teal': [20, 184, 166]
  };
  if (!hex || !/^#[0-9a-f]{6}$/i.test(hex)) return 'cyan';
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  let best = 'cyan', bestD = Infinity;
  Object.entries(colors).forEach(([name, [cr, cg, cb]]) => {
    const d = (cr-r)**2 + (cg-g)**2 + (cb-b)**2;
    if (d < bestD) { bestD = d; best = name; }
  });
  return best;
}

function showChangePasswordModal() {
  showModal(`
    <h3>🔑 Change <span class="accent">password</span></h3>
    <div class="field"><label>Current password</label><input type="password" id="cp-old"></div>
    <div class="field"><label>New password (6+ chars)</label><input type="password" id="cp-new"></div>
    <button class="btn primary" onclick="doChangePassword()">Update password</button>
    <p id="cp-msg" class="form-msg"></p>
  `);
}

async function doChangePassword() {
  const old_password = document.getElementById('cp-old').value;
  const new_password = document.getElementById('cp-new').value;
  const res = await api('/api/change_password', { method: 'POST', body: { old_password, new_password } });
  const msg = document.getElementById('cp-msg');
  if (!res.ok) {
    msg.textContent = res.data.error || 'Failed';
    return;
  }
  msg.className = 'form-msg success';
  msg.textContent = 'Password updated!';
  setTimeout(closeModal, 1500);
}

async function showEnable2FAModal() {
  showModal(`<h3>🔐 <span class="accent">Enable 2FA</span></h3><p>Generating QR code…</p>`);
  const res = await api('/api/2fa/setup', { method: 'POST' });
  if (!res.ok) return toast(res.data.error, 'error');
  showModal(`
    <h3>🔐 Enable <span class="accent">2FA</span></h3>
    <p>1. Scan this QR code with Google Authenticator, Authy, or any TOTP app.</p>
    <div class="qr-wrap">
      <img src="${res.data.qr}">
      <div class="qr-secret">${esc(res.data.secret)}</div>
    </div>
    <p>2. Enter the 6-digit code from your app:</p>
    <div class="field"><input type="text" id="tf-code" maxlength="6" inputmode="numeric" placeholder="123456"></div>
    <button class="btn primary" onclick="confirmEnable2FA()">Enable 2FA</button>
    <p id="tf-msg" class="form-msg"></p>
  `);
}

async function confirmEnable2FA() {
  const code = document.getElementById('tf-code').value.trim();
  const res = await api('/api/2fa/enable', { method: 'POST', body: { code } });
  if (!res.ok) {
    document.getElementById('tf-msg').textContent = res.data.error;
    return;
  }
  currentUser.totp_enabled = true;
  toast('2FA enabled!', 'success');
  closeModal();
}

function showDisable2FAModal() {
  showModal(`
    <h3>Disable <span class="accent">2FA</span></h3>
    <p>Enter your password to disable 2FA.</p>
    <div class="field"><input type="password" id="d2-pw"></div>
    <button class="btn danger" onclick="doDisable2FA()">Disable 2FA</button>
    <p id="d2-msg" class="form-msg"></p>
  `);
}

async function doDisable2FA() {
  const password = document.getElementById('d2-pw').value;
  const res = await api('/api/2fa/disable', { method: 'POST', body: { password } });
  if (!res.ok) return document.getElementById('d2-msg').textContent = res.data.error;
  currentUser.totp_enabled = false;
  toast('2FA disabled', 'success');
  closeModal();
}

function showDeleteAccountModal() {
  showModal(`
    <h3 style="color: var(--danger);">Delete account</h3>
    <p><b>This will permanently delete:</b></p>
    <ul style="color: var(--text-dim); margin: 10px 0 16px 20px; font-size: 13px; line-height: 1.8;">
      <li>Your account and profile</li>
      <li>All your Shards and purchases</li>
      <li>All your affiliate codes</li>
      <li>Your messages (marked as deleted)</li>
      <li>Your recovery keys</li>
    </ul>
    <p style="color: var(--danger);">This cannot be undone.</p>
    <div class="field"><input type="text" id="del-confirm" placeholder='Type "DELETE" to confirm'></div>
    <button class="btn danger" onclick="doDeleteAccount()">Delete my account forever</button>
  `);
}

async function doDeleteAccount() {
  const confirm = document.getElementById('del-confirm').value;
  const res = await api('/api/delete_account', { method: 'POST', body: { confirm } });
  if (!res.ok) return toast(res.data.error || 'Failed', 'error');
  toast('Account deleted', 'success');
  setTimeout(() => location.reload(), 1500);
}

// ==========  MY PROFILE (the me-card is the button now)  ==========
document.getElementById('me-card').addEventListener('click', () => {
  showUserProfile(currentUser.username);
});

// ==========  SHARDS HISTORY  ==========
function showShardsModal() {
  api('/api/shards/history').then(res => {
    const tx = res.data.transactions || [];
    const rows = tx.map(t => {
      const positive = t.amount >= 0;
      return `
        <div class="tx-row">
          <div class="tx-desc">${esc(t.description || t.transaction_type)}</div>
          <div class="tx-time">${new Date(t.created_at).toLocaleDateString()}</div>
          <div class="tx-amount ${positive ? 'positive' : 'negative'}">${positive ? '+' : ''}${t.amount}</div>
        </div>
      `;
    }).join('');
    showModal(`
      <h3>💎 <span class="accent">Shard history</span></h3>
      <div class="shop-header">
        <div class="shop-balance">💎 ${(currentUser.shards || 0).toLocaleString()}</div>
        <span style="color: var(--text-dim); font-size: 12px;">Current balance</span>
      </div>
      ${rows || '<p style="text-align: center; color: var(--text-mute); padding: 20px;">No transactions yet</p>'}
    `);
  });
}

// ==========  SHOP  ==========
let currentShopCategory = 'profile';
let shopItemsCache = [];

document.getElementById('shop-btn').addEventListener('click', showShopModal);

async function showShopModal() {
  const res = await api('/api/shop/items');
  shopItemsCache = res.data.items || [];
  renderShopModal();
}

function renderShopModal() {
  const isCores = shopCurrency === 'cores';
  // Every Cores item lives in the 'cores_shop' category; the Shards side has
  // its own five tabs. Showing shard tabs while filtering on cores currency
  // (or vice versa) is exactly how the shop ended up looking empty.
  const categories = isCores
    ? [{ key: 'cores_shop', label: 'Cores Shop' }]
    : [
        { key: 'profile', label: 'Profile' },
        { key: 'avatar_effects', label: 'Effects' },
        { key: 'chat', label: 'Chat' },
        { key: 'badges', label: 'Badges' },
        { key: 'perks', label: 'Perks' }
      ];
  const catButtons = categories.map(c =>
    `<button class="shop-cat ${c.key === currentShopCategory ? 'active' : ''}" onclick="switchShopCat('${c.key}')">${c.label}</button>`
  ).join('');

  const items = shopItemsCache.filter(i => isCores
    ? (i.currency || 'shards') === 'cores'
    : (i.category === currentShopCategory && (i.currency || 'shards') !== 'cores'));
  const cards = items.map(item => renderShopItem(item)).join('');

  showModal(`
    <h3>🛒 <span class="accent">Shop</span></h3>
    <div class="shop-header">
      <div class="shop-balance">💎 ${(currentUser.shards || 0).toLocaleString()} &nbsp;·&nbsp; 🪙 ${(currentUser.cores || 0).toLocaleString()}</div>
      <div class="shop-toggle">
        <button class="shop-cur ${shopCurrency === 'shards' ? 'active' : ''}" onclick="switchShopCurrency('shards')">💎 Shards</button>
        <button class="shop-cur ${shopCurrency === 'cores' ? 'active' : ''}" onclick="switchShopCurrency('cores')">🪙 Cores</button>
      </div>
    </div>
    <div class="shop-categories">${catButtons}</div>
    <div class="shop-grid">${cards || '<p style="grid-column: 1/-1; text-align: center; color: var(--text-mute); padding: 20px;">No items in this category</p>'}</div>
  `, true, true);
}

function switchShopCat(cat) {
  currentShopCategory = cat;
  renderShopModal();
}

function switchShopCurrency(cur) {
  shopCurrency = cur;
  // Keep the category on the correct side of the shop when flipping currency.
  if (cur === 'cores') currentShopCategory = 'cores_shop';
  else if (currentShopCategory === 'cores_shop') currentShopCategory = 'profile';
  renderShopModal();
}

function renderShopItem(item) {
  const owned = item.owned;
  const equipped = item.equipped;
  const cur = (item.currency || 'shards');
  const curIcon = cur === 'cores' ? '🪙' : '💎';
  const balance = cur === 'cores' ? (currentUser.cores || 0) : (currentUser.shards || 0);
  const canAfford = balance >= item.price;
  const isPerk = item.category === 'perks';
  const isCosmeticEquippable = ['avatar_effects', 'badges', 'chat'].includes(item.category);

  let badge = '';
  if (equipped) badge = '<div class="shop-badge-equipped">Equipped</div>';
  else if (owned) badge = '<div class="shop-badge-owned">Owned</div>';

  let footerBtn = '';
  if (owned && isCosmeticEquippable) {
    // Special handling for chat items with options
    if (item.item_key === 'chat_nickname_font') {
      footerBtn = `
        <select onchange="equipChatOption('${item.id}', 'chat_nickname_font', this.value)" style="width: auto; padding: 4px 8px; margin: 0; font-size: 12px;">
          <option value="">— None —</option>
          <option value="italic" ${currentUser.active_nickname_font === 'italic' ? 'selected' : ''}>Italic</option>
          <option value="bold" ${currentUser.active_nickname_font === 'bold' ? 'selected' : ''}>Bold</option>
          <option value="mono" ${currentUser.active_nickname_font === 'mono' ? 'selected' : ''}>Monospace</option>
        </select>
      `;
    } else if (item.item_key === 'chat_send_animation') {
      footerBtn = `
        <select onchange="equipChatOption('${item.id}', 'chat_send_animation', this.value)" style="width: auto; padding: 4px 8px; margin: 0; font-size: 12px;">
          <option value="">— None —</option>
          <option value="slide" ${currentUser.active_message_animation === 'slide' ? 'selected' : ''}>Slide</option>
          <option value="fade" ${currentUser.active_message_animation === 'fade' ? 'selected' : ''}>Fade</option>
          <option value="bounce" ${currentUser.active_message_animation === 'bounce' ? 'selected' : ''}>Bounce</option>
        </select>
      `;
    } else if (item.item_key === 'chat_bubble_colors') {
      footerBtn = `
        <input type="color" onchange="equipChatOption('${item.id}', 'chat_bubble_colors', this.value)" value="${esc(currentUser.active_bubble_color || '#00d9ff')}" style="width: 40px; height: 32px; padding: 2px; margin: 0;">
      `;
    } else {
      footerBtn = `<button class="btn-mini ${equipped ? 'ghost' : 'success'}" onclick="equipItem('${item.id}', ${!equipped})">${equipped ? 'Unequip' : 'Equip'}</button>`;
    }
  } else if (owned && item.item_key === 'profile_picture_upload') {
    footerBtn = `<button class="btn-mini success" onclick="uploadAvatarFlow()">Upload avatar</button>`;
  } else if (owned && item.item_key === 'profile_bio') {
    footerBtn = `<button class="btn-mini ghost" onclick="closeModal(); showSettingsModal();">Edit bio</button>`;
  } else if (owned && !isPerk) {
    footerBtn = `<span style="color: var(--success); font-size: 11px;">Owned</span>`;
  } else if (owned && isPerk) {
    footerBtn = `<button class="btn-mini ${canAfford ? '' : 'ghost'}" ${canAfford ? '' : 'disabled'} onclick="buyItem('${item.id}')">Renew</button>`;
  } else {
    footerBtn = `<button class="btn-mini ${canAfford ? '' : 'ghost'}" ${canAfford ? '' : 'disabled'} onclick="buyItem('${item.id}')">Buy</button>`;
  }

  return `
    <div class="shop-item ${owned ? 'owned' : ''} ${equipped ? 'equipped' : ''} ${cur === 'cores' ? 'cores-card' : ''}">
      ${badge}
      <div class="shop-item-icon">${esc(item.icon || '💎')}</div>
      <div class="shop-item-name">${esc(item.name)}</div>
      <div class="shop-item-desc">${esc(item.description || '')}</div>
      <div class="shop-item-footer">
        <div class="shop-item-price">${curIcon} ${item.price}</div>
        ${footerBtn}
      </div>
    </div>
  `;
}

async function buyItem(itemId) {
  const res = await api(`/api/shop/buy/${itemId}`, { method: 'POST' });
  if (!res.ok) return toast(res.data.error || 'Purchase failed', 'error');
  toast('Purchased!', 'success');
  if (typeof res.data.new_balance === 'number') {
    if (res.data.currency === 'cores') {
      currentUser.cores = res.data.new_balance;
      document.getElementById('cores-count').textContent = res.data.new_balance.toLocaleString();
    } else {
      currentUser.shards = res.data.new_balance;
      document.getElementById('shards-count').textContent = res.data.new_balance.toLocaleString();
    }
  }
  const r = await api('/api/shop/items');
  shopItemsCache = r.data.items || [];
  // The spec asks every purchase to offer a "go use it now" button
  if (res.data.next_action && res.data.next_action.action && res.data.next_action.action !== 'none') {
    const na = res.data.next_action;
    showModal(`
      <h3>✨ <span class="accent">Purchased!</span></h3>
      <p style="color: var(--text-mute); margin: 8px 0 16px;">Nice. Want to put it to work right away?</p>
      <div class="modal-actions">
        <button class="btn primary" onclick='runNextAction(${esc(JSON.stringify(na))})'>${esc(na.label || 'Use it now')}</button>
        <button class="btn secondary" onclick="closeModal(); showShopModal();">Back to shop</button>
      </div>
    `);
    return;
  }
  renderShopModal();
}

function runNextAction(na) {
  closeModal();
  switch (na.action) {
    case 'admin_panel': document.getElementById('admin-btn').click(); break;
    case 'settings': showSettingsModal(); break;
    case 'avatar': uploadAvatarFlow(); break;
    case 'bots': showMyBotsModal(); break;
    case 'spotlight': showSpotlightSetup(na); break;
    case 'equip_chat': showShopModal(); switchShopCat('chat'); break;
    case 'group_icon': toast('Pick a group and change its icon from the chat header.', 'info', 6000); break;
    default: showShopModal();
  }
}

async function equipItem(itemId, equip) {
  const res = await api(`/api/shop/equip/${itemId}`, { method: 'POST', body: { equip } });
  if (!res.ok) return toast(res.data.error || 'Failed', 'error');
  toast(equip ? 'Equipped' : 'Unequipped', 'success');
  // Refresh user to get updated effects/badges
  await refreshCurrentUser();
  // Refresh shop
  const r = await api('/api/shop/items');
  shopItemsCache = r.data.items || [];
  renderShopModal();
}

async function equipChatOption(itemId, itemKey, value) {
  const res = await api(`/api/shop/equip/${itemId}`, { method: 'POST', body: { equip: !!value, value } });
  if (!res.ok) return toast(res.data.error || 'Failed', 'error');
  await refreshCurrentUser();
  toast('Applied', 'success');
}

async function refreshCurrentUser() {
  const res = await api('/api/me');
  if (res.data.encryption) serverEncryption = res.data.encryption;
  if (res.data.user) {
    currentUser = res.data.user;
    // Update me-avatar effects
    const av = document.getElementById('me-avatar');
    av.className = 'avatar lg';
    (currentUser.active_effects || []).forEach(ef => av.classList.add(ef));
    if (currentUser.avatar_url) {
      av.innerHTML = `<img src="${esc(currentUser.avatar_url)}" alt="">`;
      av.style.background = '';
    } else {
      av.innerHTML = '';
      av.textContent = currentUser.username[0].toUpperCase();
      av.style.background = `linear-gradient(135deg, ${currentUser.nickname_color || '#00d9ff'}, #6366f1)`;
    }
    document.getElementById('shards-count').textContent = (currentUser.shards || 0).toLocaleString();
    msgListHash = ''; // force re-render to apply new bubble color/animation
  }
}

function uploadAvatarFlow() {
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = 'image/*';
  input.onchange = async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    if (!f.type.startsWith('image/')) return toast('Only images', 'error');
    toast('Uploading...', 'info');
    try {
      const compressed = await compressImage(f, 400, 0.75);
      const res = await api('/api/shop/upload_avatar', { method: 'POST', body: { image_data: compressed } });
      if (!res.ok) return toast(res.data.error || 'Upload failed', 'error');
      currentUser.avatar_url = res.data.avatar_url;
      const av = document.getElementById('me-avatar');
      av.innerHTML = `<img src="${esc(res.data.avatar_url)}" alt="">`;
      av.style.background = '';
      toast('Avatar updated!', 'success');
      const r = await api('/api/shop/items');
      shopItemsCache = r.data.items || [];
      renderShopModal();
    } catch (err) {
      toast('Failed to process image', 'error');
    }
  };
  input.click();
}

// ==========  AFFILIATE  ==========
document.getElementById('affiliate-btn').addEventListener('click', showAffiliateModal);

async function showAffiliateModal() {
  const res = await api('/api/affiliate/my_codes');
  const codes = res.data.codes || [];
  const totalEarned = codes.reduce((sum, c) => sum + (c.total_earned || 0), 0);

  const codeCards = codes.map(c => {
    let statusPill = '';
    if (c.revoked) statusPill = '<span class="affiliate-code-status aff-status-revoked">Revoked</span>';
    else if (c.rejected_at) statusPill = '<span class="affiliate-code-status aff-status-rejected">Rejected</span>';
    else if (c.approved) statusPill = '<span class="affiliate-code-status aff-status-active">Active</span>';
    else statusPill = '<span class="affiliate-code-status aff-status-pending">Pending</span>';
    return `
      <div class="affiliate-code-card">
        <div>
          <span class="affiliate-code-key">${esc(c.code)}</span>${statusPill}
          <div style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
            ${c.uses || 0} uses · ${c.total_earned || 0} 💎 earned
          </div>
        </div>
        <div>
          ${c.approved && !c.revoked ? `<button class="btn-mini ghost" onclick="copyAffCode('${esc(c.code)}')">Copy link</button>` : ''}
          ${!c.revoked ? `<button class="btn-mini danger" onclick="revokeAffCode('${c.id}')">Revoke</button>` : ''}
        </div>
      </div>
    `;
  }).join('');

  showModal(`
    <h3>🤝 <span class="accent">Affiliate program</span></h3>
    <div class="shop-header">
      <div>
        <div style="font-family: 'JetBrains Mono', monospace; color: var(--shard); font-weight: 700; font-size: 18px;">💎 ${totalEarned}</div>
        <div style="color: var(--text-mute); font-size: 11px;">Total earned from referrals</div>
      </div>
      <button class="btn primary" style="width: auto; padding: 10px 16px;" onclick="showCreateAffModal()">Create code</button>
    </div>
    ${codeCards || '<p style="text-align: center; color: var(--text-mute); padding: 20px;">No affiliate codes yet. Create one to start earning Shards!</p>'}
  `);
}

function showCreateAffModal() {
  showModal(`
    <h3>Create <span class="accent">affiliate code</span></h3>
    <p>Choose a memorable code. Friends who sign up with it earn you Shards.</p>
    <div class="field">
      <label>Code (4-20 chars, letters/numbers/underscore)</label>
      <input type="text" id="aff-code" maxlength="20" placeholder="MYCODE" autofocus>
    </div>
    <div class="field">
      <label>Reason (required if approval needed)</label>
      <textarea id="aff-reason" rows="2" maxlength="200" placeholder="Why do you want this code?"></textarea>
    </div>
    <button class="btn primary" onclick="doCreateAff()">Create</button>
    <p id="aff-msg" class="form-msg"></p>
  `);
}

async function doCreateAff() {
  const code = document.getElementById('aff-code').value.trim();
  const reason = document.getElementById('aff-reason').value.trim();
  if (!code) return document.getElementById('aff-msg').textContent = 'Code required';
  const res = await api('/api/affiliate/create', { method: 'POST', body: { code, reason } });
  if (!res.ok) return document.getElementById('aff-msg').textContent = res.data.error || 'Failed';
  if (res.data.approved) {
    toast('Affiliate code active!', 'success');
  } else {
    toast('Submitted for approval', 'info');
  }
  closeModal();
  setTimeout(showAffiliateModal, 300);
}

function copyAffCode(code) {
  const url = window.location.origin + '/?affiliate=' + code;
  copyText(url);
}

async function revokeAffCode(id) {
  const res = await api(`/api/affiliate/revoke/${id}`, { method: 'POST' });
  if (!res.ok) return toast(res.data.error || 'Failed', 'error');
  toast('Revoked', 'success');
  showAffiliateModal();
}

// ==========  LEADERBOARD  ==========
let leaderboardSort = 'shards';
document.getElementById('leaderboard-btn').addEventListener('click', () => {
  leaderboardSort = 'shards';
  showLeaderboardModal();
});

async function showLeaderboardModal() {
  const res = await api('/api/leaderboard?sort=' + leaderboardSort);
  const users = res.data.users || [];
  const rows = users.map((u, i) => {
    const rank = i + 1;
    const rankClass = rank <= 3 ? `rank-${rank}` : '';
    return `
      <div class="lb-row">
        <div class="lb-rank ${rankClass}">${rank}</div>
        ${avatarHtml(u, 'sm')}
        <div class="lb-user">
          <div class="lb-username" onclick="showUserProfile('${esc(u.username)}')" style="color: ${esc(u.nickname_color || '#00d9ff')}">${esc(u.username)}</div>
          <div class="lb-badges">${badgesHtml(u.active_badges)}</div>
        </div>
        <div class="lb-shards">💎 ${(u.shards || 0).toLocaleString()}</div>
        <div class="lb-refs">${u.referrals || 0} refs</div>
      </div>
    `;
  }).join('');

  const sortBtn = (key, label) =>
    `<button class="btn-mini ${leaderboardSort === key ? 'active' : 'ghost'}" onclick="changeLbSort('${key}')">${label}</button>`;

  showModal(`
    <h3>🏆 <span class="accent">Leaderboard</span></h3>
    <div class="lb-sort">
      ${sortBtn('shards', 'Most Shards')}
      ${sortBtn('referrals', 'Most Referrals')}
      ${sortBtn('newest', 'Newest')}
      ${sortBtn('oldest', 'Oldest')}
    </div>
    ${rows || '<p style="text-align: center; color: var(--text-mute); padding: 20px;">No users to show</p>'}
    ${currentUser.leaderboard_opt_out ? '<p style="text-align: center; color: var(--text-mute); font-size: 11px; margin-top: 16px;">You are hidden from the leaderboard. Change in Settings to compete.</p>' : ''}
  `, true, true);
}

function changeLbSort(sort) {
  leaderboardSort = sort;
  showLeaderboardModal();
}

// ==========  PUBLIC PROFILE CARD  ==========
async function showUserProfile(username) {
  const res = await api('/api/profile/' + encodeURIComponent(username));
  if (!res.ok) return toast(res.data.error || 'Failed to load profile', 'error');
  const u = res.data.user;
  const memberSince = u.created_at ? formatDate(u.created_at) : 'unknown';
  const streamer = currentUser.streamer_mode && currentUser.streamer_mode.enabled;

  // Streamer mode blurs identity on screen
  const blurClass = streamer && !u.is_self ? ' streamer-blur' : '';

  const avatar = avatarHtml(u, 'xl');
  const badges = u.active_badges && u.active_badges.length
    ? `<div class="profile-badges">${badgesHtml(u.active_badges, { size: 'lg' })}</div>`
    : '';
  const bio = u.bio ? `<div class="profile-bio">${esc(u.bio)}</div>` : '';
  const stats = u.hidden ? '' : `
    <div class="profile-stats">
      <div class="profile-stat">
        <div class="profile-stat-num">💎 ${(u.shards || 0).toLocaleString()}</div>
        <div class="profile-stat-label">Shards</div>
      </div>
      <div class="profile-stat">
        <div class="profile-stat-num">${u.total_messages_sent != null ? u.total_messages_sent.toLocaleString() : '—'}</div>
        <div class="profile-stat-label">Messages</div>
      </div>
      <div class="profile-stat">
        <div class="profile-stat-num">${u.referrals != null ? u.referrals : '—'}</div>
        <div class="profile-stat-label">Referrals</div>
      </div>
      <div class="profile-stat">
        <div class="profile-stat-num">${u.no_warning_streak_days != null ? u.no_warning_streak_days + 'd' : '—'}</div>
        <div class="profile-stat-label">Clean streak</div>
      </div>
    </div>
  `;

  const fs = u.friendship || { state: 'none' };
  let friendBtn = '';
  if (!u.is_self) {
    if (fs.state === 'friends') {
      friendBtn = `<button class="btn-mini ghost" onclick="unfriend('${fs.friendship_id}')">Unfriend</button>`;
    } else if (fs.state === 'pending_out') {
      friendBtn = `<button class="btn-mini ghost" disabled>Requested…</button>`;
    } else if (fs.state === 'pending_in') {
      friendBtn = `<button class="btn-mini success" onclick="respondFriend('${fs.friendship_id}', true)">Accept request</button>`;
    } else {
      friendBtn = `<button class="btn-mini" onclick="requestFriend('${esc(u.username)}')">Add friend</button>`;
    }
  }

  const actions = u.is_self ? `
    <div class="profile-actions">
      <button class="btn secondary" onclick="closeProfileCard(); showSettingsModal();">Edit profile</button>
    </div>` : `
    <div class="profile-actions">
      <button class="btn primary" onclick="closeProfileCard(); messageUser('${esc(u.username)}')">💬 Message</button>
      <button class="btn secondary" onclick="showGiftModal('${esc(u.username)}')">🎁 Gift</button>
      ${friendBtn}
      <button class="btn-mini danger" onclick="reportUser('${esc(u.username)}')">Report</button>
    </div>`;

  showProfileCard(`
    <div class="profile-banner" style="background: linear-gradient(135deg, ${esc(u.banner_color || u.nickname_color || '#00d9ff')}, #0b0e14)"></div>
    <div class="profile-card-body${blurClass}">
      ${avatar}
      <div class="profile-name" style="color: ${esc(u.nickname_color || '#00d9ff')}${u.name_font ? '' : ''}">${esc(u.username)}${u.is_bot ? ' <span class="bot-tag">BOT</span>' : ''}</div>
      <div class="profile-since">Member since ${memberSince}</div>
      ${badges}
      ${bio}
      ${stats}
      ${actions}
    </div>
  `);
}

// ==========  ADMIN PANEL  ==========
document.getElementById('admin-btn').addEventListener('click', () => {
  const isOwner = currentUser.is_owner;
  const perms = currentUser.permissions || {};

  showModal(`
    <h3>👑 Admin <span class="accent">panel</span></h3>
    <div class="admin-tabs">
      <button class="admin-tab active" onclick="switchAdmin(this,'stats')">Stats</button>
      <button class="admin-tab" onclick="switchAdmin(this,'users')">Users</button>
      <button class="admin-tab" onclick="switchAdmin(this,'bans')">IP Bans</button>
      <button class="admin-tab" onclick="switchAdmin(this,'ann')">Announcements</button>
      <button class="admin-tab" onclick="switchAdmin(this,'aff')">Affiliates</button>
      ${(isOwner || perms.can_manage_shop_items) ? `<button class="admin-tab" onclick="switchAdmin(this,'shop')">Shop mgmt</button>` : ''}
      ${(isOwner || perms.can_view_messages) ? `<button class="admin-tab" onclick="switchAdmin(this,'emerg')">Emergency</button>` : ''}
      ${isOwner ? `<button class="admin-tab" onclick="switchAdmin(this,'rights')">Admin Rights</button>` : ''}
      ${isOwner ? `<button class="admin-tab" onclick="switchAdmin(this,'set')">Settings</button>` : ''}
      ${isOwner || perms.can_grant_cores ? `<button class="admin-tab" onclick="switchAdmin(this,'cores')">Cores</button>` : ''}
      ${isOwner ? `<button class="admin-tab" onclick="switchAdmin(this,'badges')">Badges</button>` : ''}
      ${isOwner || perms.can_suspend_ban_users ? `<button class="admin-tab" onclick="switchAdmin(this,'bots')">Bots</button>` : ''}
      ${isOwner ? `<button class="admin-tab" onclick="switchAdmin(this,'ask')">Ask Nicely</button>` : ''}
      ${isOwner ? `<button class="admin-tab" onclick="switchAdmin(this,'apps')">Applications</button>` : ''}
      ${isOwner ? `<button class="admin-tab" onclick="switchAdmin(this,'padm')">Purchased admins</button>` : ''}
      <button class="admin-tab" onclick="switchAdmin(this,'audit')">Audit log</button>
      <button class="admin-tab" onclick="switchAdmin(this,'spam')">Spam</button>
    </div>
    <div id="admin-body"></div>
  `, true, true);
  loadAdminStats();
});

function switchAdmin(btn, tab) {
  document.querySelectorAll('.admin-tab').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  const fn = {
    stats: loadAdminStats,
    users: loadAdminUsers,
    bans: loadAdminBans,
    ann: loadAdminAnn,
    aff: loadAdminAffiliates,
    shop: loadAdminShop,
    emerg: loadAdminEmergency,
    rights: loadAdminRights,
    set: loadAdminSettings,
    cores: loadAdminCores,
    badges: loadAdminBadges,
    bots: loadAdminBots,
    ask: loadAdminAskNicely,
    apps: loadAdminApplications,
    padm: loadAdminPurchased,
    audit: loadAdminAudit,
    spam: loadAdminSpam
  }[tab];
  if (fn) fn();
}

async function loadAdminStats() {
  const body = document.getElementById('admin-body');
  body.innerHTML = 'Loading...';
  const res = await api('/api/admin/stats');
  const s = res.data;
  body.innerHTML = `
    <div class="stats-grid">
      <div class="stat-card"><div class="stat-num">${s.users || 0}</div><div class="stat-label">Users</div></div>
      <div class="stat-card"><div class="stat-num">${s.active_24h || 0}</div><div class="stat-label">Active 24h</div></div>
      <div class="stat-card"><div class="stat-num">${s.messages || 0}</div><div class="stat-label">Messages</div></div>
      <div class="stat-card"><div class="stat-num">${s.conversations || 0}</div><div class="stat-label">Chats</div></div>
      <div class="stat-card"><div class="stat-num">${s.bans || 0}</div><div class="stat-label">IP Bans</div></div>
    </div>
  `;
}

// ==========  ADMIN USERS with search + pagination  ==========
let adminUsersOffset = 0;
let adminUsersSearch = '';
let adminUsersLoaded = [];

async function loadAdminUsers(reset = true) {
  const body = document.getElementById('admin-body');
  if (reset) {
    adminUsersOffset = 0;
    adminUsersLoaded = [];
    body.innerHTML = `
      <div class="admin-search">
        <input type="text" id="user-search" placeholder="Search users by username..." value="${esc(adminUsersSearch)}">
        <button class="btn-mini" onclick="doAdminUserSearch()">Search</button>
        <button class="btn-mini ghost" onclick="clearAdminUserSearch()">Clear</button>
      </div>
      <div id="admin-users-list">Loading...</div>
    `;
    document.getElementById('user-search').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') doAdminUserSearch();
    });
  }

  const url = `/api/admin/users?offset=${adminUsersOffset}&limit=20${adminUsersSearch ? '&search=' + encodeURIComponent(adminUsersSearch) : ''}`;
  const res = await api(url);
  if (!res.ok) return;

  const newUsers = res.data.users || [];
  adminUsersLoaded = adminUsersLoaded.concat(newUsers);
  adminUsersOffset += newUsers.length;

  renderAdminUsersList(res.data.total, res.data.has_more);
}

function doAdminUserSearch() {
  adminUsersSearch = document.getElementById('user-search').value.trim();
  loadAdminUsers(true);
}
function clearAdminUserSearch() {
  adminUsersSearch = '';
  document.getElementById('user-search').value = '';
  loadAdminUsers(true);
}

function renderAdminUsersList(total, hasMore) {
  const rows = adminUsersLoaded.map(u => {
    let badges = '';
    if (u.is_owner) badges += '<span class="badge owner">Owner</span>';
    if (u.is_admin && !u.is_owner) badges += '<span class="badge admin">Admin</span>';
    if (u.is_immune) badges += '<span class="badge immune">Immune</span>';
    if (u.suspended) badges += '<span class="badge suspended">Suspended</span>';
    if (u.totp_enabled) badges += '<span class="badge info">2FA</span>';
    if ((u.throttle_level || 0) > 0) badges += `<span class="badge warn">Throttle L${u.throttle_level}</span>`;
    if ((u.spam_warnings || 0) > 0) badges += `<span class="badge critical">${u.spam_warnings} warns</span>`;

    let actions = '';
    if (!u.is_immune) {
      actions += `<button class="btn-mini" onclick="adminToggle('${u.id}','suspended',${!u.suspended})">${u.suspended ? 'Unsuspend' : 'Suspend'}</button>`;
      if (currentUser.is_owner) {
        actions += `<button class="btn-mini" onclick="adminToggle('${u.id}','is_admin',${!u.is_admin})">${u.is_admin ? '-Admin' : '+Admin'}</button>`;
      }
      actions += `<button class="btn-mini warn" onclick="adminPunish('${u.id}','${esc(u.username)}')">Punish</button>`;
      actions += `<button class="btn-mini danger" onclick="adminBanAccount('${u.id}','${esc(u.username)}')">Ban acc</button>`;
      actions += `<button class="btn-mini" onclick="adminResetPw('${u.id}')">Reset PW</button>`;
      actions += `<button class="btn-mini danger" onclick="adminBanIP('${esc(u.last_ip || '')}')">Ban IP</button>`;
      if (currentUser.is_owner && !u.is_owner) {
        actions += `<button class="btn-mini danger" onclick="ownerDeleteUser('${u.id}','${esc(u.username)}')">Delete</button>`;
      }
    } else {
      actions = '<span style="color: var(--text-mute); font-size: 11px;">Immune (protected)</span>';
    }

    return `
      <tr>
        <td>
          <span style="color: ${esc(u.nickname_color)}; font-weight: 500; cursor: pointer;" onclick="showUserProfile('${esc(u.username)}')">${esc(u.username)}</span>
          ${badges}
          <div style="color: var(--text-mute); font-size: 10px; margin-top: 2px;">💎 ${u.shards || 0}</div>
        </td>
        <td><code style="font-size: 11px;">${esc(u.last_ip || '-')}</code></td>
        <td style="font-size: 11px; color: var(--text-mute);">${u.last_seen ? new Date(u.last_seen).toLocaleDateString() : '-'}</td>
        <td>${actions}</td>
      </tr>
    `;
  }).join('');

  const listEl = document.getElementById('admin-users-list');
  listEl.innerHTML = `
    <table>
      <thead><tr><th>User</th><th>IP</th><th>Last seen</th><th>Actions</th></tr></thead>
      <tbody>${rows || '<tr class="empty-row"><td colspan="4">No users found</td></tr>'}</tbody>
    </table>
    ${hasMore ? `
      <div class="load-more-wrap">
        <div class="info">Showing ${adminUsersLoaded.length} of ${total}</div>
        <button class="btn-mini" onclick="loadAdminUsers(false)">Load 20 more</button>
      </div>
    ` : (adminUsersLoaded.length > 0 ? `<div class="load-more-wrap"><div class="info">Showing all ${adminUsersLoaded.length} of ${total}</div></div>` : '')}
  `;
}

async function adminToggle(uid, field, val) {
  const res = await api(`/api/admin/user/${uid}`, { method: 'POST', body: { [field]: val } });
  if (!res.ok) return toast(res.data.error, 'error');
  toast('Updated', 'success');
  loadAdminUsers(true);
}

async function adminResetPw(uid) {
  showModal(`
    <h3>Reset password?</h3>
    <p>A new random password will be generated. Give it to the user.</p>
    <button class="btn danger" onclick="confirmResetPw('${uid}')">Yes, reset it</button>
    <button class="btn secondary" style="margin-top: 8px;" onclick="closeModal()">Cancel</button>
  `);
}

async function confirmResetPw(uid) {
  const res = await api(`/api/admin/user/${uid}/reset_password`, { method: 'POST' });
  if (!res.ok) return toast(res.data.error, 'error');
  showModal(`
    <h3>New password generated</h3>
    <p>Give this password to the user. They should change it after logging in.</p>
    <div class="recovery-content">${esc(res.data.new_password)}</div>
    <button class="btn primary" onclick="copyText('${esc(res.data.new_password)}'); closeModal();">Copy & close</button>
  `);
}

function adminPunish(uid, username) {
  showModal(`
    <h3>Punish <span class="accent">${esc(username)}</span></h3>
    <div class="field">
      <label>Type</label>
      <select id="p-type">
        <option value="warn">Warning</option>
        <option value="mute">Mute (can't send messages)</option>
        <option value="ban">Ban (can't log in)</option>
      </select>
    </div>
    <div class="field"><label>Reason</label><input type="text" id="p-reason"></div>
    <div class="field"><label>Duration in hours (blank = permanent)</label><input type="number" id="p-hours"></div>
    <button class="btn primary" onclick="doPunish('${uid}')">Apply</button>
    <h4>History</h4>
    <div id="p-history">Loading...</div>
  `);
  loadPunishmentHistory(uid);
}

async function loadPunishmentHistory(uid) {
  const res = await api(`/api/admin/user/${uid}/punishments`);
  const box = document.getElementById('p-history');
  const puns = res.data.punishments || [];
  if (!puns.length) { box.innerHTML = '<p style="color: var(--text-mute);">No history</p>'; return; }
  box.innerHTML = puns.map(p => `
    <div style="padding: 8px; border-bottom: 1px solid var(--border); font-size: 12px;">
      <b>${p.type.toUpperCase()}</b> ${p.active ? '(active)' : '(inactive)'} — ${esc(p.reason || 'no reason')}
      <span style="float: right; color: var(--text-mute); font-size: 10px;">${new Date(p.created_at).toLocaleDateString()}</span>
      ${p.active ? `<button class="btn-mini" onclick="removePunishment('${p.id}','${uid}')" style="margin-left: 8px;">Remove</button>` : ''}
    </div>
  `).join('');
}

async function doPunish(uid) {
  const body = {
    type: document.getElementById('p-type').value,
    reason: document.getElementById('p-reason').value.trim(),
    hours: parseInt(document.getElementById('p-hours').value) || null
  };
  const res = await api(`/api/admin/user/${uid}/punish`, { method: 'POST', body });
  if (!res.ok) return toast(res.data.error, 'error');
  toast('Punishment applied', 'success');
  loadPunishmentHistory(uid);
}

async function removePunishment(pid, uid) {
  await api(`/api/admin/punishment/${pid}/remove`, { method: 'POST' });
  toast('Removed', 'success');
  loadPunishmentHistory(uid);
}

// Ban account (I3)
async function adminBanAccount(uid, username) {
  showModal(`
    <h3>Ban <span class="accent">${esc(username)}</span>'s account</h3>
    <p>This will block them from logging in on ANY device or IP.</p>
    <div class="field"><label>Reason</label><input type="text" id="ba-reason"></div>
    <div class="field"><label>Duration in hours (blank = permanent)</label><input type="number" id="ba-hours"></div>
    <button class="btn danger" onclick="doBanAccount('${uid}')">Ban account</button>
  `);
}

async function doBanAccount(uid) {
  const body = {
    reason: document.getElementById('ba-reason').value.trim(),
    hours: parseInt(document.getElementById('ba-hours').value) || null
  };
  const res = await api(`/api/admin/user/${uid}/ban_account`, { method: 'POST', body });
  if (!res.ok) return toast(res.data.error, 'error');
  toast('Account banned', 'success');
  closeModal();
  loadAdminUsers(true);
}

// Owner delete user (I4)
function ownerDeleteUser(uid, username) {
  const expected = `DELETE @${username}`;
  showModal(`
    <h3 style="color: var(--danger)">Delete @${esc(username)}'s account?</h3>
    <p>This PERMANENTLY deletes their account, all their messages, Shards, purchases, and profile.</p>
    <p style="color: var(--danger);">This cannot be undone.</p>
    <div class="field"><label>Type <code>${esc(expected)}</code> to confirm</label>
      <input type="text" id="del-user-confirm" placeholder="${esc(expected)}">
    </div>
    <button class="btn danger" onclick="doOwnerDeleteUser('${uid}','${esc(expected)}')">Delete permanently</button>
    <button class="btn secondary" style="margin-top: 8px;" onclick="closeModal()">Cancel</button>
  `);
}

async function doOwnerDeleteUser(uid, expected) {
  const confirm = document.getElementById('del-user-confirm').value;
  if (confirm !== expected) return toast('Confirmation does not match', 'error');
  const res = await api(`/api/admin/user/${uid}/delete`, { method: 'POST', body: { confirm } });
  if (!res.ok) return toast(res.data.error || 'Failed', 'error');
  toast('User deleted', 'success');
  closeModal();
  loadAdminUsers(true);
}

async function adminBanIP(ip) {
  if (!ip || ip === '-') return toast('No IP available', 'error');
  showModal(`
    <h3>Ban IP <code>${esc(ip)}</code>?</h3>
    <div class="field"><label>Reason (optional)</label><input type="text" id="banip-reason"></div>
    <button class="btn danger" onclick="doAdminBanIP('${esc(ip)}')">Ban IP</button>
  `);
}

async function doAdminBanIP(ip) {
  const reason = document.getElementById('banip-reason').value.trim();
  const res = await api('/api/admin/ban', { method: 'POST', body: { ip, reason } });
  if (!res.ok) return toast(res.data.error, 'error');
  toast('IP banned', 'success');
  closeModal();
}

async function loadAdminBans() {
  const body = document.getElementById('admin-body');
  const res = await api('/api/admin/bans');
  const bans = res.data.bans || [];
  body.innerHTML = `
    <div style="display: flex; gap: 8px; margin-bottom: 16px;">
      <input type="text" id="ban-ip" placeholder="IP address" style="flex: 1; margin: 0;">
      <input type="text" id="ban-reason" placeholder="Reason" style="flex: 1; margin: 0;">
      <button class="btn primary" style="width: auto; padding: 11px 18px;" onclick="manualBan()">Ban</button>
    </div>
    <table>
      <thead><tr><th>IP</th><th>Reason</th><th></th></tr></thead>
      <tbody>${bans.map(b => `
        <tr>
          <td><code>${esc(b.ip_address)}</code></td>
          <td>${esc(b.reason || '-')}</td>
          <td><button class="btn-mini" onclick="unbanIP('${esc(b.ip_address)}')">Unban</button></td>
        </tr>
      `).join('') || '<tr class="empty-row"><td colspan="3">No bans</td></tr>'}</tbody>
    </table>
  `;
}

async function manualBan() {
  const ip = document.getElementById('ban-ip').value.trim();
  const reason = document.getElementById('ban-reason').value.trim();
  if (!ip) return;
  const res = await api('/api/admin/ban', { method: 'POST', body: { ip, reason } });
  if (!res.ok) return toast(res.data.error, 'error');
  toast('Banned', 'success');
  loadAdminBans();
}

async function unbanIP(ip) {
  await api('/api/admin/unban', { method: 'POST', body: { ip } });
  toast('Unbanned', 'success');
  loadAdminBans();
}

async function loadAdminAnn() {
  const body = document.getElementById('admin-body');
  const res = await api('/api/announcements');
  const anns = res.data.announcements || [];
  body.innerHTML = `
    <div class="field"><label>Title</label><input type="text" id="ann-title"></div>
    <div class="field"><label>Content</label><textarea id="ann-content" rows="2"></textarea></div>
    <div class="field">
      <label>Priority</label>
      <select id="ann-pri">
        <option value="info">Info (blue)</option>
        <option value="warn">Warning (yellow)</option>
        <option value="critical">Critical (red)</option>
      </select>
    </div>
    <button class="btn primary" onclick="postAnn()">Post announcement</button>
    <h4>Active</h4>
    <table><tbody>${anns.map(a => `
      <tr>
        <td>
          <span class="badge ${a.priority}">${a.priority}</span>
          <b>${esc(a.title)}</b><br>
          <span style="color: var(--text-dim); font-size: 12px;">${esc(a.content)}</span>
        </td>
        <td style="text-align: right;"><button class="btn-mini danger" onclick="deleteAnn('${a.id}')">Delete</button></td>
      </tr>
    `).join('') || '<tr class="empty-row"><td colspan="2">No active announcements</td></tr>'}</tbody></table>
  `;
}

async function postAnn() {
  const body = {
    title: document.getElementById('ann-title').value.trim(),
    content: document.getElementById('ann-content').value.trim(),
    priority: document.getElementById('ann-pri').value
  };
  if (!body.title || !body.content) return toast('Fill both fields', 'error');
  const res = await api('/api/announcements', { method: 'POST', body });
  if (!res.ok) return toast(res.data.error, 'error');
  toast('Posted', 'success');
  dismissedAnns = [];
  localStorage.setItem('cipher_dismissed_anns', '[]');
  loadAnnouncements();
  loadAdminAnn();
}

async function deleteAnn(id) {
  await api(`/api/announcements/${id}`, { method: 'DELETE' });
  toast('Deleted', 'success');
  loadAdminAnn();
  loadAnnouncements();
}

// ==========  ADMIN: Affiliate approval  ==========
async function loadAdminAffiliates() {
  const body = document.getElementById('admin-body');
  const res = await api('/api/admin/affiliate/pending');
  if (!res.ok) {
    // Show the real backend error (schema drift used to look like a
    // permissions problem to the owner).
    body.innerHTML = `<p>${esc(res.data.error || 'You do not have permission to view this.')}</p>`;
    return;
  }
  const pending = res.data.pending || [];
  body.innerHTML = `
    <h4>Pending affiliate code requests</h4>
    ${pending.map(p => `
      <div class="affiliate-code-card">
        <div>
          <span class="affiliate-code-key">${esc(p.code)}</span>
          <div style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
            by @${esc(p.users?.username || '?')} · ${new Date(p.created_at).toLocaleDateString()}
          </div>
          ${p.reason ? `<div style="color: var(--text); font-size: 12px; margin-top: 6px; padding: 6px 8px; background: var(--bg-3); border-radius: 6px;">${esc(p.reason)}</div>` : ''}
        </div>
        <div>
          <button class="btn-mini success" onclick="approveAff('${p.id}')">Approve</button>
          <button class="btn-mini danger" onclick="rejectAff('${p.id}')">Reject</button>
        </div>
      </div>
    `).join('') || '<p style="color: var(--text-mute); padding: 20px; text-align: center;">No pending requests</p>'}
  `;
}

async function approveAff(id) {
  const res = await api(`/api/admin/affiliate/${id}/approve`, { method: 'POST' });
  if (!res.ok) return toast(res.data.error || 'Failed', 'error');
  toast('Approved', 'success');
  loadAdminAffiliates();
}

async function rejectAff(id) {
  const reason = window.prompt('Rejection reason (optional):') || '';
  const res = await api(`/api/admin/affiliate/${id}/reject`, { method: 'POST', body: { reason } });
  if (!res.ok) return toast(res.data.error || 'Failed', 'error');
  toast('Rejected', 'success');
  loadAdminAffiliates();
}

// ==========  ADMIN: Shop management  ==========
async function loadAdminShop() {
  const body = document.getElementById('admin-body');
  const res = await api('/api/admin/shop/items');
  if (!res.ok) { body.innerHTML = '<p>You do not have permission.</p>'; return; }
  const items = res.data.items || [];
  body.innerHTML = `
    <table>
      <thead><tr><th>Item</th><th>Category</th><th>Price</th><th>Enabled</th><th></th></tr></thead>
      <tbody>${items.map(i => `
        <tr>
          <td>${esc(i.icon || '💎')} ${esc(i.name)}<br><code style="font-size: 10px;">${esc(i.item_key)}</code></td>
          <td>${esc(i.category)}</td>
          <td>
            <input type="number" value="${i.price}" style="width: 80px; margin: 0; padding: 4px 6px;" id="shop-price-${i.id}">
          </td>
          <td>
            <input type="checkbox" ${i.enabled ? 'checked' : ''} id="shop-enabled-${i.id}">
          </td>
          <td><button class="btn-mini" onclick="saveShopItem('${i.id}')">Save</button></td>
        </tr>
      `).join('')}</tbody>
    </table>
  `;
}

async function saveShopItem(id) {
  const price = parseInt(document.getElementById('shop-price-' + id).value) || 0;
  const enabled = document.getElementById('shop-enabled-' + id).checked;
  const res = await api(`/api/admin/shop/items/${id}`, { method: 'POST', body: { price, enabled } });
  if (!res.ok) return toast(res.data.error, 'error');
  toast('Saved', 'success');
}

// ==========  ADMIN: Emergency viewer  ==========
async function loadAdminEmergency() {
  const body = document.getElementById('admin-body');
  const isOwner = currentUser.is_owner;
  body.innerHTML = `
    ${!isOwner ? `<div class="emergency-warning"><strong>⚠️ Admin access is logged.</strong> Every conversation you view here will be recorded with your reason. Only use this for legitimate abuse investigation.</div>` : ''}
    <div class="field"><input type="text" id="emerg-search" placeholder="Search user by username..."></div>
    <div id="emerg-results"></div>
    ${isOwner ? '<button class="btn secondary" style="margin-top: 20px;" onclick="loadOwnerAccessLog()">View admin access history</button>' : ''}
    <div id="emerg-access-log"></div>
  `;
  document.getElementById('emerg-search').addEventListener('input', (e) => {
    clearTimeout(window._emergSearch);
    window._emergSearch = setTimeout(() => doEmergencySearch(e.target.value.trim()), 300);
  });
}

async function doEmergencySearch(q) {
  const box = document.getElementById('emerg-results');
  if (!q) { box.innerHTML = ''; return; }
  const res = await api('/api/admin/emergency/search?q=' + encodeURIComponent(q));
  if (!res.ok) { box.innerHTML = ''; return; }
  const users = res.data.users || [];
  box.innerHTML = users.map(u => `
    <div class="emergency-user-result" onclick="loadEmergencyUserConvs('${u.id}','${esc(u.username)}')">
      ${avatarHtml(u, 'sm')}
      <div>
        <div style="color: ${esc(u.nickname_color || '#00d9ff')}; font-weight: 500;">${esc(u.username)}</div>
        <div style="color: var(--text-mute); font-size: 11px;">Last seen: ${u.last_seen ? new Date(u.last_seen).toLocaleDateString() : 'never'}</div>
      </div>
    </div>
  `).join('') || '<p style="color: var(--text-mute); padding: 12px;">No users found</p>';
}

async function loadEmergencyUserConvs(uid, username) {
  const box = document.getElementById('emerg-results');
  box.innerHTML = `<h4>Conversations for ${esc(username)}</h4><p>Loading...</p>`;
  const res = await api('/api/admin/emergency/user_conversations/' + uid);
  if (!res.ok) { box.innerHTML = '<p>Failed to load</p>'; return; }
  const convs = res.data.conversations || [];
  box.innerHTML = `
    <h4>Conversations for ${esc(username)}</h4>
    <button class="btn-mini ghost" onclick="doEmergencySearch(document.getElementById('emerg-search').value.trim())">← Back to search</button>
    <div style="margin-top: 12px;">
      ${convs.map(c => `
        <div class="emergency-conv-item" onclick="viewEmergencyMessages('${c.id}','${uid}','${esc(username)}')">
          <b>${esc(c.name || (c.is_group ? 'Group chat' : 'DM'))}</b>
          <div style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
            ${c.members.map(m => esc(m)).join(', ')}
          </div>
          <div style="color: var(--text-mute); font-size: 10px; margin-top: 2px;">
            Updated: ${new Date(c.updated_at).toLocaleString()}
          </div>
        </div>
      `).join('') || '<p style="color: var(--text-mute);">No conversations</p>'}
    </div>
  `;
}

async function viewEmergencyMessages(cid, targetUid, targetUsername) {
  const isOwner = currentUser.is_owner;
  let reason = '';
  if (!isOwner) {
    reason = window.prompt(`Reason for viewing this conversation (minimum 10 characters):`, '') || '';
    if (reason.trim().length < 10) {
      return toast('Reason must be at least 10 characters', 'error');
    }
  }

  const res = await api('/api/admin/emergency/messages', {
    method: 'POST',
    body: { conversation_id: cid, target_user_id: targetUid, reason }
  });
  if (!res.ok) return toast(res.data.error || 'Failed', 'error');

  const msgs = res.data.messages || [];
  const html = msgs.map(m => {
    const senderName = m.users?.username || '[deleted user]';
    const time = new Date(m.created_at).toLocaleString();
    const content = m.content ? esc(m.content) : (m.image_url ? '[image]' : '[empty]');
    const anonNote = m.is_anonymous ? ' <span class="anonymous-tag">was anon</span>' : '';
    const deletedNote = m.deleted ? ' <span style="color: var(--danger); font-size: 10px;">[deleted]</span>' : '';
    return `
      <div class="emergency-msg">
        <div class="emergency-msg-header">
          <span class="emergency-msg-sender">${esc(senderName)}${anonNote}${deletedNote}</span>
          <span>${time}</span>
        </div>
        <div>${content}</div>
      </div>
    `;
  }).join('');

  const box = document.getElementById('emerg-results');
  box.innerHTML = `
    <h4>Messages (${msgs.length}) — ${esc(targetUsername)}'s chat</h4>
    <button class="btn-mini ghost" onclick="loadEmergencyUserConvs('${targetUid}','${esc(targetUsername)}')">← Back to conversations</button>
    <div style="margin-top: 12px; max-height: 500px; overflow-y: auto;">${html || '<p>No messages</p>'}</div>
  `;
}

async function loadOwnerAccessLog() {
  const box = document.getElementById('emerg-access-log');
  const res = await api('/api/admin/emergency/access_log');
  if (!res.ok) { box.innerHTML = '<p>Failed to load</p>'; return; }
  const logs = res.data.logs || [];
  box.innerHTML = `
    <h4>Admin access history</h4>
    ${logs.map(l => `
      <div style="padding: 10px; border-bottom: 1px solid var(--border); font-size: 12px;">
        <b>${esc(l.viewer_username)}</b> viewed
        ${l.target_username ? '@' + esc(l.target_username) + "'s" : 'a'} conversation
        <div style="color: var(--text-dim); margin-top: 4px;">Reason: ${esc(l.reason || '(none)')}</div>
        <div style="color: var(--text-mute); font-size: 10px; margin-top: 2px; font-family: monospace;">
          ${new Date(l.created_at).toLocaleString()} · IP: ${esc(l.ip || '-')}
        </div>
      </div>
    `).join('') || '<p style="color: var(--text-mute); padding: 12px;">No access logged</p>'}
  `;
}

// ==========  ADMIN: Admin Rights (owner only)  ==========
async function loadAdminRights() {
  const body = document.getElementById('admin-body');
  const res = await api('/api/admin/admin_rights');
  if (!res.ok) { body.innerHTML = '<p>Owner only.</p>'; return; }
  const admins = res.data.admins || [];
  const permLabels = {
    can_view_messages: 'View messages (Emergency)',
    can_approve_affiliates: 'Approve affiliates',
    can_create_announcements: 'Create announcements',
    can_ban_ips: 'Ban IPs',
    can_suspend_ban_users: 'Suspend/ban users',
    can_reset_passwords: 'Reset passwords',
    can_manage_shop_items: 'Manage shop',
    can_manage_admins: 'Manage admins'
  };
  body.innerHTML = `
    <p>Toggle what each admin can do. Owner has all permissions.</p>
    ${admins.map(a => `
      <div class="admin-list-header">
        ${avatarHtml(a, 'sm')}
        <div style="flex: 1;">
          <b style="color: ${esc(a.nickname_color)}">${esc(a.username)}</b>
          ${a.is_owner ? '<span class="badge owner">Owner</span>' : ''}
        </div>
      </div>
      ${a.is_owner ? '<p style="color: var(--text-mute); padding: 12px;">Owner has all permissions by default.</p>' : `
        <div class="rights-grid">
          ${Object.entries(permLabels).map(([k, label]) => `
            <label class="right-toggle">
              <span>${label}</span>
              <input type="checkbox" ${a.permissions[k] ? 'checked' : ''} onchange="updateRight('${a.id}','${k}',this.checked)">
            </label>
          `).join('')}
        </div>
      `}
    `).join('')}
  `;
}

async function updateRight(uid, key, val) {
  const res = await api(`/api/admin/admin_rights/${uid}`, { method: 'POST', body: { [key]: val } });
  if (!res.ok) return toast(res.data.error, 'error');
  toast(val ? 'Enabled' : 'Disabled', 'success');
}

// ==========  ADMIN: Settings  ==========
async function loadAdminSettings() {
  const body = document.getElementById('admin-body');
  const res = await api('/api/admin/settings');
  if (!res.ok) { body.innerHTML = '<p>Owner only.</p>'; return; }
  const s = res.data.settings || {};
  body.innerHTML = `
    <div class="setting-row">
      <div class="setting-info">
        <div class="setting-label">Signups enabled</div>
        <div class="setting-desc">Allow new users to register</div>
      </div>
      <input type="checkbox" id="ss-signups" ${s.signups_enabled ? 'checked' : ''}>
    </div>
    <div class="setting-row">
      <div class="setting-info">
        <div class="setting-label">Invites enabled</div>
        <div class="setting-desc">Allow users to create invite links</div>
      </div>
      <input type="checkbox" id="ss-invites" ${s.invites_enabled ? 'checked' : ''}>
    </div>
    <div class="setting-row">
      <div class="setting-info">
        <div class="setting-label">Invite creation mode</div>
        <div class="setting-desc">Who can make invites</div>
      </div>
      <select id="ss-mode">
        <option value="everyone" ${s.invite_creation_mode === 'everyone' ? 'selected' : ''}>Everyone</option>
        <option value="admins_only" ${s.invite_creation_mode === 'admins_only' ? 'selected' : ''}>Admins only</option>
      </select>
    </div>
    <div class="setting-row">
      <div class="setting-info">
        <div class="setting-label">Affiliate mode</div>
        <div class="setting-desc">How affiliate codes are created</div>
      </div>
      <select id="ss-affmode">
        <option value="everyone" ${s.affiliate_mode === 'everyone' ? 'selected' : ''}>Everyone</option>
        <option value="requires_approval" ${s.affiliate_mode === 'requires_approval' ? 'selected' : ''}>Requires approval</option>
        <option value="owner_only" ${s.affiliate_mode === 'owner_only' ? 'selected' : ''}>Owner only</option>
      </select>
    </div>
    <div class="setting-row">
      <div class="setting-info">
        <div class="setting-label">Shards per referral</div>
        <div class="setting-desc">Reward when someone uses your affiliate code</div>
      </div>
      <input type="number" id="ss-shards" value="${s.shards_per_referral || 10}">
    </div>
    <div class="setting-row">
      <div class="setting-info">
        <div class="setting-label">Message retention (days)</div>
        <div class="setting-desc">How long messages last by default</div>
      </div>
      <input type="number" id="ss-days" value="${s.default_retention_days || 30}">
    </div>
    <div class="setting-row">
      <div class="setting-info">
        <div class="setting-label">Maintenance mode</div>
        <div class="setting-desc">Restrict access to admins only</div>
      </div>
      <input type="checkbox" id="ss-maint" ${s.maintenance_mode ? 'checked' : ''}>
    </div>
    <button class="btn primary" style="margin-top: 16px;" onclick="saveAdminSettings()">Save settings</button>
  `;
}

async function saveAdminSettings() {
  const body = {
    signups_enabled: document.getElementById('ss-signups').checked,
    invites_enabled: document.getElementById('ss-invites').checked,
    invite_creation_mode: document.getElementById('ss-mode').value,
    affiliate_mode: document.getElementById('ss-affmode').value,
    shards_per_referral: parseInt(document.getElementById('ss-shards').value) || 10,
    default_retention_days: parseInt(document.getElementById('ss-days').value) || 30,
    maintenance_mode: document.getElementById('ss-maint').checked
  };
  const res = await api('/api/admin/settings', { method: 'POST', body });
  if (!res.ok) return toast(res.data.error, 'error');
  toast('Saved', 'success');
}

async function loadAdminAudit() {
  const body = document.getElementById('admin-body');
  const res = await api('/api/admin/audit');
  const logs = res.data.logs || [];
  body.innerHTML = logs.map(l => `
    <div class="audit-entry">
      <span class="audit-who">${esc(l.admin_username || 'system')}</span>
      <span class="audit-action">${esc(l.action)}</span>
      ${l.target_type ? `<span class="audit-target">on ${esc(l.target_type)}</span>` : ''}
      ${l.details ? `<div style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">${esc(l.details).slice(0, 120)}</div>` : ''}
      <span class="audit-time">${new Date(l.created_at).toLocaleString()} · ${esc(l.ip_address || '')}</span>
    </div>
  `).join('') || '<p style="text-align: center; color: var(--text-mute); padding: 20px;">No audit entries yet</p>';
}

async function loadAdminSpam() {
  const body = document.getElementById('admin-body');
  const res = await api('/api/admin/spam_events');
  const events = res.data.events || [];
  body.innerHTML = `
    <p>Users get warnings 1-5 before permanent IP ban.</p>
    <table>
      <thead><tr><th>User</th><th>Warning #</th><th>Reason</th><th>When</th></tr></thead>
      <tbody>${events.map(e => `
        <tr>
          <td>${esc(e.users?.username || '?')}</td>
          <td><span class="badge ${e.warning_number >= 4 ? 'critical' : 'warn'}">${e.warning_number}</span></td>
          <td>${esc(e.trigger_reason || '-')}</td>
          <td style="font-size: 11px; color: var(--text-mute);">${new Date(e.created_at).toLocaleString()}</td>
        </tr>
      `).join('') || '<tr class="empty-row"><td colspan="4">No spam events</td></tr>'}</tbody>
    </table>
  `;
}

// ==========  ANNOUNCEMENTS BANNER  ==========
async function loadAnnouncements() {
  const res = await api('/api/announcements');
  const anns = res.data.announcements || [];
  const bar = document.getElementById('announcement-bar');
  const next = anns.find(a => !dismissedAnns.includes(a.id));
  if (!next) {
    bar.classList.add('hidden');
    return;
  }
  bar.className = `announcement-bar ${next.priority}`;
  bar.innerHTML = `
    <span><b>${esc(next.title)}:</b> ${esc(next.content)}</span>
    <button class="close-ann" type="button" onclick="dismissAnnouncement('${next.id}')">×</button>
  `;
  bar.classList.remove('hidden');
}

function dismissAnnouncement(id) {
  dismissedAnns.push(id);
  localStorage.setItem('cipher_dismissed_anns', JSON.stringify(dismissedAnns));
  document.getElementById('announcement-bar').classList.add('hidden');
}


// ============================================================
//   v1.2 UI — HyperCrypt, economy, social, safety, admin
// ============================================================
let serverEncryption = null;
let pendingUnlockPassword = null;

// ---------- small helpers ----------
function paintAvatar(el, user, size) {
  if (!el) return;
  el.className = `avatar ${size} ${((user && user.active_effects) || []).join(' ')}`;
  el.textContent = '';
  el.style.background = '';
  el.onclick = null;
  if (user && user.avatar_url) {
    el.innerHTML = `<img src="${esc(user.avatar_url)}" alt="">`;
  } else {
    el.textContent = (((user && user.username) || '?')[0] || '?').toUpperCase();
    el.style.background = `linear-gradient(135deg, ${(user && user.nickname_color) || '#00d9ff'}, #6366f1)`;
  }
}

function isScrolledToBottom(box) {
  return box.scrollHeight - box.scrollTop - box.clientHeight < 100;
}

function formatDateDivider(d) {
  const today = new Date();
  const yest = new Date(Date.now() - 86400000);
  if (d.toDateString() === today.toDateString()) return 'Today';
  if (d.toDateString() === yest.toDateString()) return 'Yesterday';
  return d.toLocaleDateString([], { weekday: 'long', month: 'short', day: 'numeric' });
}

function updateEmptyConversations() {
  const list = document.getElementById('conv-list');
  let hint = document.getElementById('conv-empty-hint');
  if (!hint) {
    hint = document.createElement('div');
    hint.id = 'conv-empty-hint';
    hint.className = 'conv-empty';
    hint.innerHTML = 'No conversations yet.<br>Start one with the + button.';
    list.parentNode.appendChild(hint);
  }
  hint.classList.toggle('hidden', list.children.length > 0);
}

function computePollInterval() {
  if (document.hidden) return pollConfig.hidden_ms;
  if (idleCycles >= pollConfig.very_idle_after_cycles) return pollConfig.very_idle_ms;
  if (idleCycles >= pollConfig.idle_after_cycles) return pollConfig.idle_ms;
  return pollConfig.active_ms;
}

function restartPollingWithNewInterval() {
  if (pollTimer) clearTimeout(pollTimer);
  const tick = async () => {
    if (!currentConv) return;
    await loadMessages();
    await checkTyping();
    pollTimer = setTimeout(tick, computePollInterval());
  };
  pollTimer = setTimeout(tick, computePollInterval());
}

// ---------- encryption plumbing ----------
function cryptoMembers() {
  const meta = currentConvMeta || {};
  return (meta.members || [])
    .filter(m => m && m.id && m.username)
    .map(m => ({ id: m.id, username: m.username }));
}

function masterPublicKey() {
  return (serverEncryption && serverEncryption.master_key) || null;
}

async function initEncryption() {
  if (!window.HyperCrypt || !HyperCrypt.isSupported()) {
    encryptionReady = false;
    return;
  }
  if (serverEncryption && serverEncryption.master_key) {
    HyperCrypt.setMasterKey(serverEncryption.master_key);
  }
  try {
    await HyperCrypt.ensureIdentity(api, pendingUnlockPassword || '', currentUser.username);
    pendingUnlockPassword = null;
    encryptionReady = true;
    try { await HyperCrypt.loadAllKeys(api); } catch (e) {}
    const st = document.getElementById('s-enc-status');
    if (st) st.textContent = 'Active on this device';
    // Re-decrypt any previews now that keys exist
    decryptedPreview = {};
    loadConversations();
  } catch (err) {
    if (err && err.needPassword) {
      showUnlockModal();
    } else {
      encryptionReady = false;
      console.warn('[HyperCrypt]', err);
    }
  }
}

function showUnlockModal() {
  showModal(`
    <h3>🔐 <span class="accent">Unlock HyperCrypt</span></h3>
    <p style="color: var(--text-mute); margin-bottom: 12px;">
      This account has an encryption identity from another device.
      Type your password to restore it — the password never leaves this device.
    </p>
    <div class="field">
      <label>Password</label>
      <input type="password" id="unlock-password" placeholder="Your password">
    </div>
    <p id="unlock-msg" class="form-msg"></p>
    <button class="btn primary" onclick="doUnlock()">Unlock</button>
  `);
  const go = (e) => { if (e.key === 'Enter') doUnlock(); };
  document.getElementById('unlock-password').addEventListener('keydown', go);
  document.getElementById('unlock-password').focus();
}

async function doUnlock() {
  const pw = document.getElementById('unlock-password').value;
  pendingUnlockPassword = pw;
  closeModal();
  await initEncryption();
  if (!encryptionReady) {
    toast('Could not restore your key. You can try again from Settings.', 'warn', 6000);
  }
}

function showEncryptionModal() {
  const info = (window.HyperCrypt && HyperCrypt.publicInfo()) || {};
  const fp = info.fingerprint || '—';
  showModal(`
    <h3>🔐 <span class="accent">How your messages stay private</span></h3>
    <div class="tos-content">
      <p>Messages are sealed on your device with a key only you and the person
      you chat with hold. The server stores ciphertext it cannot read.</p>
      <p>Your identity key never leaves this device unencrypted; the copy on the
      server is wrapped with a key derived from your password.</p>
      <p class="dim">Key fingerprint: <code>${esc(fp)}</code></p>
      <p class="dim">Full details live in the Terms of Service and the
      "How it works" panel.</p>
    </div>
  `, true);
}

async function encryptImageForSend(dataUrl) {
  // dataUrl -> ArrayBuffer -> HyperCrypt ciphertext (base64)
  const res = await fetch(dataUrl);
  const buf = await res.arrayBuffer();
  return await HyperCrypt.encryptImage(api, currentConv, cryptoMembers(), buf);
}

// ---------- daily bonus ----------
async function refreshDailyButton() {
  const btn = document.getElementById('daily-btn');
  if (!btn) return;
  const res = await api('/api/shards/daily');
  if (res.ok && res.data.available) btn.classList.remove('hidden');
  else btn.classList.add('hidden');
}

async function claimDailyBonus() {
  const res = await api('/api/shards/daily', { method: 'POST' });
  if (!res.ok) return toast(res.data.error || 'Not available yet', 'warn');
  if (typeof res.data.balance === 'number') {
    currentUser.shards = res.data.balance;
    document.getElementById('shards-count').textContent = res.data.balance.toLocaleString();
  }
  toast(res.data.message || '+1 Shard 🎁', 'success');
  refreshDailyButton();
}

function showCoresModal() {
  api('/api/cores/history').then(res => {
    const tx = res.data.transactions || [];
    const rows = tx.map(t => `
      <tr>
        <td>${esc(t.reason || t.type || 'grant')}</td>
        <td style="color:${(t.amount || 0) >= 0 ? 'var(--success)' : 'var(--danger)'}">${(t.amount || 0) >= 0 ? '+' : ''}${t.amount}</td>
        <td style="color: var(--text-mute); font-size: 11px;">${new Date(t.created_at).toLocaleString()}</td>
      </tr>`).join('');
    showModal(`
      <h3>🪙 <span class="accent">Cores</span> — <span style="color:var(--gold)">${(currentUser.cores || 0).toLocaleString()}</span></h3>
      <p style="color: var(--text-mute); margin-bottom: 10px;">Cores are granted by a person, never earned by grinding. Spend them in the Cores shop tab.</p>
      <table class="table"><thead><tr><th>Reason</th><th>Amount</th><th>When</th></tr></thead>
      <tbody>${rows || '<tr class="empty-row"><td colspan="3">No Core activity yet</td></tr>'}</tbody></table>
      <button class="btn secondary" onclick="showShopModal(); switchShopCurrency('cores');">Open the Cores shop</button>
    `, true, true);
  });
}

// ---------- badges catalog ----------
async function loadBadgeCatalog() {
  const res = await api('/api/badges/catalog');
  if (res.ok && res.data.badges) {
    badgeCatalog = res.data.badges;
  }
}

// ---------- spotlight ----------
async function loadSpotlight() {
  const bar = document.getElementById('spotlight-bar');
  if (!bar) return;
  const res = await api('/api/spotlight');
  const sp = res.data.spotlight;
  if (!sp) { bar.classList.add('hidden'); return; }
  const u = sp.users || {};
  const name = (currentUser.streamer_mode && currentUser.streamer_mode.enabled) ? 'Someone' : (u.username || 'Someone');
  bar.innerHTML = `
    <span class="spotlight-star">✨</span>
    <span class="spotlight-text"><b style="color:${esc(u.nickname_color || '#ffd700')}">${esc(name)}</b> is in the spotlight${sp.message ? ` — “${esc(sp.message)}”` : ''}</span>
    <button class="link small" type="button" onclick="showUserProfile('${esc(u.username || '')}')">Say hi</button>
  `;
  bar.classList.remove('hidden');
}

function showSpotlightSetup(na) {
  showModal(`
    <h3>✨ <span class="accent">Start your spotlight</span></h3>
    <p style="color: var(--text-mute); margin-bottom: 10px;">You own a spotlight coupon. Pick what the banner says (or leave it classic).</p>
    <div class="field">
      <label>Banner message (optional, max 100 chars)</label>
      <input type="text" id="sp-msg" maxlength="100" placeholder="e.g. just hit a 30-day streak 🔥">
    </div>
    <p id="sp-msg-err" class="form-msg"></p>
    <button class="btn primary" onclick="startSpotlight('${esc((na && na.item_key) || 'cores_spotlight_basic')}')">Go live for 24h</button>
  `);
}

async function startSpotlight(itemKey) {
  const msg = (document.getElementById('sp-msg') || {}).value || '';
  const res = await api('/api/spotlight', { method: 'POST', body: { item_key: itemKey, message: msg } });
  if (!res.ok) {
    document.getElementById('sp-msg-err').textContent = res.data.error || 'Failed';
    return;
  }
  toast('You are in the spotlight ✨', 'success');
  closeModal();
  loadSpotlight();
}

// ---------- notifications ----------
async function pollNotifications() {
  const res = await api('/api/notifications');
  const badge = document.getElementById('notifications-badge');
  if (badge) {
    const n = res.data.unread || 0;
    badge.textContent = n > 99 ? '99+' : n;
    badge.classList.toggle('hidden', n === 0);
  }
  if (notificationTimer) clearTimeout(notificationTimer);
  notificationTimer = setTimeout(pollNotifications, 30000);
}

function showNotificationsModal() {
  api('/api/notifications').then(res => {
    const notes = res.data.notifications || [];
    const rows = notes.map(nt => `
      <div class="note-row ${nt.read ? '' : 'unread'}" onclick="readNote('${nt.id}')">
        <div class="note-icon">${esc(nt.icon || '🔔')}</div>
        <div class="note-body">
          <div class="note-title">${esc(nt.title || 'Notification')}</div>
          <div class="note-text">${esc(nt.body || '')}</div>
        </div>
        <div class="note-time">${new Date(nt.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</div>
      </div>`).join('');
    showModal(`
      <h3>🔔 <span class="accent">Notifications</span></h3>
      <div class="note-list">${rows || '<p style="color: var(--text-mute); text-align:center; padding: 20px;">Nothing yet.</p>'}</div>
      <button class="btn secondary" onclick="api('/api/notifications/read_all', { method: 'POST' }).then(() => { pollNotifications(); showNotificationsModal(); })">Mark all read</button>
    `, true, true);
  });
}

async function readNote(id) {
  await api(`/api/notifications/${id}/read`, { method: 'POST' });
  pollNotifications();
  showNotificationsModal();
}

// ---------- friends ----------
async function refreshFriendBadge() {
  const res = await api('/api/friends');
  const badge = document.getElementById('friends-badge');
  if (!badge) return;
  const n = (res.data.incoming || []).length;
  badge.textContent = n;
  badge.classList.toggle('hidden', n === 0);
}

function showFriendsModal() {
  api('/api/friends').then(res => {
    const incoming = res.data.incoming || [];
    const friends = res.data.friends || [];
    const outgoing = res.data.outgoing || [];
    const person = (f, extra) => `
      <div class="friend-row">
        <div class="avatar" style="background: linear-gradient(135deg, ${esc(f.nickname_color || '#00d9ff')}, #6366f1)">${esc((f.username || '?')[0].toUpperCase())}</div>
        <div class="friend-name" onclick="closeModal(); showUserProfile('${esc(f.username)}')" style="cursor:pointer">${esc(f.username)}</div>
        ${extra}
      </div>`;
    const inc = incoming.map(f => person(f, `
      <button class="btn-mini success" onclick="respondFriend('${f.friendship_id}', true)">Accept</button>
      <button class="btn-mini ghost" onclick="respondFriend('${f.friendship_id}', false)">Ignore</button>`)).join('');
    const fr = friends.map(f => person(f, `
      <button class="btn-mini ghost" onclick="unfriend('${f.friendship_id}')">Remove</button>`)).join('');
    const out = outgoing.map(f => person(f, `<span class="dim" style="font-size:11px">pending</span>`)).join('');
    showModal(`
      <h3>👥 <span class="accent">Friends</span></h3>
      ${incoming.length ? `<h4>Requests</h4>${inc}` : ''}
      <h4>Your friends (${friends.length})</h4>${fr || '<p class="dim">No friends yet.</p>'}
      ${outgoing.length ? `<h4>Sent</h4>${out}` : ''}
    `, true, true);
  });
}

async function requestFriend(username) {
  const res = await api('/api/friends/request', { method: 'POST', body: { username } });
  if (!res.ok) return toast(res.data.error || 'Could not send request', 'warn');
  toast(res.data.message || 'Friend request sent', 'success');
  showUserProfile(username);
}

async function respondFriend(fid, accept) {
  const res = await api(`/api/friends/${fid}/respond`, { method: 'POST', body: { accept } });
  if (!res.ok) return toast(res.data.error || 'Failed', 'error');
  refreshFriendBadge();
  showFriendsModal();
}

async function unfriend(fid) {
  const res = await api(`/api/friends/${fid}`, { method: 'DELETE' });
  if (!res.ok) return toast(res.data.error || 'Failed', 'error');
  refreshFriendBadge();
  showFriendsModal();
}

// ---------- gifting ----------
function showGiftModal(username) {
  showModal(`
    <h3>🎁 <span class="accent">Gift Shards</span> to ${esc(username)}</h3>
    <div class="field">
      <label>Amount</label>
      <input type="number" id="gift-amount" min="1" value="5">
    </div>
    <div class="field">
      <label>Note (optional)</label>
      <input type="text" id="gift-note" maxlength="280" placeholder="Say something nice">
    </div>
    <p id="gift-msg" class="form-msg"></p>
    <button class="btn primary" onclick="sendGift('${esc(username)}')">Send gift</button>
  `);
}

async function sendGift(username) {
  const amount = parseInt(document.getElementById('gift-amount').value, 10) || 0;
  const message = document.getElementById('gift-note').value;
  const res = await api('/api/shards/gift', { method: 'POST', body: { username, amount, message } });
  const msg = document.getElementById('gift-msg');
  if (!res.ok) { msg.textContent = res.data.error || 'Failed'; return; }
  if (typeof res.data.new_balance === 'number') {
    currentUser.shards = res.data.new_balance;
    document.getElementById('shards-count').textContent = res.data.new_balance.toLocaleString();
  }
  toast(`Gifted ${amount} 💎 to ${username}`, 'success');
  closeModal();
}

// ---------- username change ----------
function showChangeUsernameModal() {
  showModal(`
    <h3>✏️ <span class="accent">Change username</span></h3>
    <p style="color: var(--text-mute); margin-bottom: 10px;">You get a few changes per hour. Old names stay retired.</p>
    <div class="field">
      <label>New username</label>
      <input type="text" id="nu-name" value="${esc(currentUser.username)}">
    </div>
    <div class="field">
      <label>Confirm password</label>
      <input type="password" id="nu-pass">
    </div>
    <p id="nu-msg" class="form-msg"></p>
    <button class="btn primary" onclick="doChangeUsername()">Change</button>
  `);
}

async function doChangeUsername() {
  const new_username = document.getElementById('nu-name').value.trim();
  const password = document.getElementById('nu-pass').value;
  const res = await api('/api/settings/username', { method: 'POST', body: { new_username, password } });
  const msg = document.getElementById('nu-msg');
  if (!res.ok) { msg.textContent = res.data.error || 'Failed'; return; }
  toast('Username changed', 'success');
  currentUser.username = res.data.username || new_username;
  document.getElementById('my-username').textContent = currentUser.username;
  closeModal();
}

// ---------- warning modal + Sorry ----------
async function showWarningModal(data) {
  const sorry = await api('/api/sorry');
  const can = sorry.data || {};
  const body = document.getElementById('modal-content');
  showModal(`
    <div class="big-warning">
      <div class="big-warning-icon">⚠️</div>
      <h3 class="big-warning-title">Whoa there</h3>
      <p class="big-warning-body">${esc(data.body || data.error || 'Something triggered the anti-cheat.')}</p>
      ${can.can_use ? `
        <button class="btn secondary" onclick="useSorry()">🙏 Sorry — undo this warning (${can.uses_available} left)</button>
      ` : (can.throttled ? '<p class="dim">You are throttled right now — slow down a little.</p>' : '')}
      <button class="btn primary" onclick="closeModal()">I understand</button>
    </div>
  `);
}

async function useSorry() {
  const res = await api('/api/sorry', { method: 'POST' });
  if (!res.ok) return toast(res.data.error || 'Sorry not available', 'warn');
  toast(res.data.message || 'Warning undone. This time. 🙏', 'success');
  closeModal();
}

// ---------- report / message ----------
function reportUser(username) {
  showModal(`
    <h3>🚩 <span class="accent">Report</span> ${esc(username)}</h3>
    <div class="field">
      <label>What happened?</label>
      <textarea id="rep-details" rows="3" placeholder="Describe the issue"></textarea>
    </div>
    <button class="btn danger" onclick="doReport('${esc(username)}')">Send report</button>
  `);
}

async function doReport(username) {
  const details = document.getElementById('rep-details').value;
  const res = await api('/api/anticheat/report', { method: 'POST', body: { event_type: 'user_report', details: `about ${username}: ${details}` } });
  if (!res.ok) return toast(res.data.error || 'Failed', 'error');
  toast('Report received. A person will look at it.', 'success');
  closeModal();
}

async function messageUser(username) {
  const res = await api('/api/conversations/new_dm', { method: 'POST', body: { username } });
  if (!res.ok) return toast(res.data.error || 'Could not open chat', 'error');
  closeModal(); closeProfileCard();
  await loadConversations();
  const cRes = await api('/api/conversations');
  const conv = (cRes.data.conversations || []).find(c => c.id === res.data.conversation_id);
  if (conv) openConversation(conv);
}

// ---------- features modal ----------
function showFeaturesModal() {
  api('/api/features').then(res => {
    const f = res.data || {};
    const enc = f.encryption || {};
    const points = (enc.points || []).map(pt => `<li>${esc(pt)}</li>`).join('');
    const notes = (f.notes || []).map(nt => `<li>${esc(nt)}</li>`).join('');
    showModal(`
      <h3>🔐 <span class="accent">${esc(enc.name || 'Cipher HyperCrypt')}</span></h3>
      <p class="tos-lead">${esc(enc.tagline || '')}</p>
      <div class="tos-section"><h4>The encryption, in human words</h4><ul class="features-list">${points}</ul></div>
      <div class="tos-section"><h4>The vibe</h4><ul class="features-list">${notes}</ul></div>
      <p class="dim" style="margin-top: 10px;">${esc(f.made_by || '')}</p>
    `, true, true);
  });
}

// ---------- bots ----------
function showMyBotsModal() {
  api('/api/bots/my_bots').then(res => {
    const bots = res.data.bots || [];
    const rows = bots.map(b => `
      <div class="friend-row">
        <div class="avatar" style="background: linear-gradient(135deg, #a78bfa, #6366f1)">🤖</div>
        <div class="friend-name">${esc(b.name)}</div>
        <button class="btn-mini ghost" onclick="toggleBot('${b.id}', ${!b.active})">${b.active ? 'Pause' : 'Resume'}</button>
        <button class="btn-mini danger" onclick="deleteBot('${b.id}')">Delete</button>
      </div>`).join('');
    showModal(`
      <h3>🤖 <span class="accent">Your bots</span></h3>
      <p style="color: var(--text-mute); margin-bottom: 10px;">Bots post through the API with a hashed token, capped at 60 messages/minute. Max 5.</p>
      ${rows || '<p class="dim">No bots yet.</p>'}
      <button class="btn primary" onclick="showCreateBot()">Create a bot</button>
    `, true, true);
  });
}

function showCreateBot() {
  showModal(`
    <h3>🤖 <span class="accent">New bot</span></h3>
    <div class="field"><label>Bot name</label><input type="text" id="bot-name" placeholder="my-cool-bot"></div>
    <p id="bot-msg" class="form-msg"></p>
    <button class="btn primary" onclick="createBot()">Create</button>
  `);
}

async function createBot() {
  const name = document.getElementById('bot-name').value.trim();
  const res = await api('/api/bots/register', { method: 'POST', body: { name } });
  const msg = document.getElementById('bot-msg');
  if (!res.ok) { msg.textContent = res.data.error || 'Failed'; return; }
  showModal(`
    <h3>🤖 <span class="accent">Bot created</span></h3>
    <p style="color: var(--text-mute);">Copy this token now — it is shown once:</p>
    <code class="token-box">${esc(res.data.token)}</code>
    <button class="btn primary" onclick="showMyBotsModal()">Done</button>
  `);
}

async function toggleBot(id, active) {
  await api(`/api/admin/bots/${id}/toggle`, { method: 'POST', body: { active } }).catch(() => {});
  // Owners/admins use the admin endpoint; regular users use their own toggle
  await api(`/api/bots/${id}/deactivate`, { method: 'POST' }).catch(() => {});
  showMyBotsModal();
}

async function deleteBot(id) {
  await api(`/api/bots/${id}`, { method: 'DELETE' });
  showMyBotsModal();
}

// ---------- admin v1.2 loaders ----------
async function loadAdminCores() {
  const body = document.getElementById('admin-body');
  body.innerHTML = `
    <div class="admin-search">
      <input type="text" id="gc-user" placeholder="username">
      <input type="number" id="gc-amount" placeholder="amount" style="width:90px">
      <input type="text" id="gc-reason" placeholder="reason" style="flex:1">
      <button class="btn-mini" onclick="grantCores()">Grant Cores</button>
    </div>
    <div id="gc-log">Loading…</div>
  `;
  const res = await api('/api/admin/cores');
  const rows = (res.data.grants || []).map(g => `
    <tr><td>${esc(g.username || g.user_id)}</td><td>${g.amount}</td><td>${esc(g.reason || '')}</td><td style="font-size:11px">${new Date(g.created_at).toLocaleString()}</td></tr>`).join('');
  document.getElementById('gc-log').innerHTML = `
    <table class="table"><thead><tr><th>User</th><th>Amount</th><th>Reason</th><th>When</th></tr></thead>
    <tbody>${rows || '<tr class="empty-row"><td colspan="4">No grants yet</td></tr>'}</tbody></table>`;
}

async function grantCores() {
  const username = document.getElementById('gc-user').value.trim();
  const amount = parseInt(document.getElementById('gc-amount').value, 10) || 0;
  const reason = document.getElementById('gc-reason').value;
  const res = await api('/api/cores/grant', { method: 'POST', body: { username, amount, reason } });
  if (!res.ok) return toast(res.data.error || 'Failed', 'error');
  toast('Cores granted', 'success');
  loadAdminCores();
}

async function loadAdminBadges() {
  const body = document.getElementById('admin-body');
  body.innerHTML = `
    <div class="admin-search">
      <input type="text" id="gb-user" placeholder="username">
      <input type="text" id="gb-badge" placeholder="badge key">
      <button class="btn-mini" onclick="grantBadge()">Assign badge</button>
    </div>
    <p class="dim" style="font-size:11px">Custom badges you create appear in everyone's catalog.</p>
    <div id="gb-list">Loading…</div>
  `;
  const res = await api('/api/admin/badges');
  const rows = (res.data.badges || []).map(b => `
    <tr><td>${esc(b.name)}</td><td>${esc(b.icon || '🏅')}</td><td>${esc(b.description || '')}</td></tr>`).join('');
  document.getElementById('gb-list').innerHTML = `
    <table class="table"><thead><tr><th>Name</th><th>Icon</th><th>Description</th></tr></thead>
    <tbody>${rows || '<tr class="empty-row"><td colspan="3">None yet</td></tr>'}</tbody></table>`;
}

async function grantBadge() {
  const username = document.getElementById('gb-user').value.trim();
  const badge = document.getElementById('gb-badge').value.trim();
  const res = await api(`/api/admin/badges/${encodeURIComponent(username)}/assign`, { method: 'POST', body: { badge } });
  if (!res.ok) return toast(res.data.error || 'Failed', 'error');
  toast('Badge assigned', 'success');
  loadAdminBadges();
}

async function loadAdminBots() {
  const body = document.getElementById('admin-body');
  body.innerHTML = '<div id="ab-list">Loading…</div>';
  const res = await api('/api/admin/bots');
  const rows = (res.data.bots || []).map(b => `
    <tr><td>${esc(b.name)}</td><td>${esc(b.owner_username || b.user_id)}</td><td>${b.messages_last_minute || 0}/60</td>
    <td>${b.active ? 'active' : 'paused'}</td>
    <td><button class="btn-mini" onclick="adminToggleBot('${b.id}', ${!b.active})">${b.active ? 'Pause' : 'Resume'}</button></td></tr>`).join('');
  document.getElementById('ab-list').innerHTML = `
    <table class="table"><thead><tr><th>Bot</th><th>Owner</th><th>Rate</th><th>Status</th><th></th></tr></thead>
    <tbody>${rows || '<tr class="empty-row"><td colspan="5">No bots</td></tr>'}</tbody></table>`;
}

async function adminToggleBot(id, active) {
  await api(`/api/admin/bots/${id}/toggle`, { method: 'POST', body: { active } });
  loadAdminBots();
}

async function loadAdminAskNicely() {
  const body = document.getElementById('admin-body');
  body.innerHTML = '<div id="an-list">Loading…</div>';
  const res = await api('/api/admin/ask_nicely');
  const rows = (res.data.requests || []).map(r => `
    <tr><td>${esc(r.username || r.user_id)}</td><td>${esc(r.message || '')}</td><td>${esc(r.status)}</td>
    <td>${r.status === 'pending' ? `
      <button class="btn-mini success" onclick="askDecision('${r.id}', true)">Grant</button>
      <button class="btn-mini danger" onclick="askDecision('${r.id}', false)">Deny</button>` : ''}</td></tr>`).join('');
  document.getElementById('an-list').innerHTML = `
    <table class="table"><thead><tr><th>User</th><th>Asked</th><th>Status</th><th></th></tr></thead>
    <tbody>${rows || '<tr class="empty-row"><td colspan="4">Nothing pending</td></tr>'}</tbody></table>`;
}

async function askDecision(id, approve) {
  await api(`/api/admin/ask_nicely/${id}/${approve ? 'approve' : 'reject'}`, { method: 'POST' });
  loadAdminAskNicely();
}

async function loadAdminApplications() {
  const body = document.getElementById('admin-body');
  body.innerHTML = '<div id="aa-list">Loading…</div>';
  const res = await api('/api/admin/applications');
  const rows = (res.data.applications || []).map(a => `
    <tr><td>${esc(a.username || a.user_id)}</td><td>${esc(a.reason || '')}</td><td>${esc(a.status)}</td>
    <td>${a.status === 'pending' ? `
      <button class="btn-mini success" onclick="appDecision('${a.id}', true)">Approve</button>
      <button class="btn-mini danger" onclick="appDecision('${a.id}', false)">Reject</button>` : ''}</td></tr>`).join('');
  document.getElementById('aa-list').innerHTML = `
    <table class="table"><thead><tr><th>User</th><th>Why</th><th>Status</th><th></th></tr></thead>
    <tbody>${rows || '<tr class="empty-row"><td colspan="4">No applications</td></tr>'}</tbody></table>`;
}

async function appDecision(id, approve) {
  await api(`/api/admin/applications/${id}/${approve ? 'approve' : 'reject'}`, { method: 'POST' });
  loadAdminApplications();
}

async function loadAdminPurchased() {
  const body = document.getElementById('admin-body');
  body.innerHTML = '<div id="ap-list">Loading…</div>';
  const res = await api('/api/admin/purchased_admins');
  const rows = (res.data.admins || []).map(a => `
    <tr><td>${esc(a.username || a.user_id)}</td><td>${esc(a.item_key || '')}</td>
    <td><button class="btn-mini danger" onclick="revokePurchasedAdmin('${a.user_id}')">Revoke</button></td></tr>`).join('');
  document.getElementById('ap-list').innerHTML = `
    <table class="table"><thead><tr><th>User</th><th>Via</th><th></th></tr></thead>
    <tbody>${rows || '<tr class="empty-row"><td colspan="3">No purchased admins</td></tr>'}</tbody></table>`;
}

async function revokePurchasedAdmin(uid) {
  await api(`/api/admin/revoke_admin/${uid}`, { method: 'POST' });
  loadAdminPurchased();
}

// ---------- sidebar button wiring ----------
document.getElementById('notifications-btn').addEventListener('click', showNotificationsModal);
document.getElementById('friends-btn').addEventListener('click', showFriendsModal);
document.getElementById('features-btn').addEventListener('click', showFeaturesModal);

// ==========  BOOTSTRAP  ==========
initAuthTabs();
checkSession();
setInterval(loadAnnouncements, 60000);
