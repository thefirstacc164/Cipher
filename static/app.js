/* ============================================================
   CIPHER v1.0.0 — Frontend
   Solo project by Stepundrik
   ============================================================ */

// ==========  STATE  ==========
let currentUser = null;
let currentUserShards = 0;
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
let lastMessageHash = '';
let idleCycles = 0;
let msgListHash = '';
let convListHash = '';
let convListTimer = null;

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
    return { ok: r.ok, status: r.status, data: j };
  } catch (err) {
    return { ok: false, status: 0, data: { error: 'Network error' } };
  }
}

function showModal(html, wide = false) {
  const modal = document.getElementById('modal');
  const bg = document.getElementById('modal-bg');
  modal.classList.toggle('wide', wide);
  document.getElementById('modal-content').innerHTML = html;
  bg.classList.remove('hidden');
}

function closeModal() {
  document.getElementById('modal-bg').classList.add('hidden');
  document.getElementById('modal-content').innerHTML = '';
}

// Close modal on background click
document.getElementById('modal-bg').addEventListener('click', (e) => {
  if (e.target.id === 'modal-bg') closeModal();
});

function updateTheme(hex) {
  if (!hex || !/^#[0-9a-f]{6}$/i.test(hex)) hex = '#00d9ff';
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  const root = document.documentElement.style;
  root.setProperty('--accent', hex);
  // Slightly darker for accent-2
  const darken = v => Math.max(0, Math.floor(v * 0.85));
  const acc2 = `#${darken(r).toString(16).padStart(2, '0')}${darken(g).toString(16).padStart(2, '0')}${darken(b).toString(16).padStart(2, '0')}`;
  root.setProperty('--accent-2', acc2);
  root.setProperty('--accent-glow', `rgba(${r},${g},${b},0.35)`);
  root.setProperty('--accent-soft', `rgba(${r},${g},${b},0.1)`);
  // Ink = dark version for text on accent background
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

// ==========  AUTH SCREEN  ==========
function initAuthTabs() {
  document.querySelectorAll('.auth-card .tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.auth-card .tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      const isSignup = tab.dataset.tab === 'signup';
      document.getElementById('invite-field').classList.toggle('hidden', !isSignup);
    const affiliateField = document.getElementById('affiliate-field');
    if (affiliateField) affiliateField.classList.toggle('hidden', !isSignup);
    const tosField = document.getElementById('tos-field');
    if (tosField) tosField.classList.toggle('hidden', !isSignup);
      document.getElementById('totp-field').classList.add('hidden');
      document.getElementById('auth-submit').textContent = isSignup ? 'Create account' : 'Sign in';
      document.getElementById('auth-error').textContent = '';
    });
  });

  // Auto-fill invite from URL
  const params = new URLSearchParams(window.location.search);
  const inv = params.get('invite');
  if (inv) {
    document.querySelector('.tab[data-tab="signup"]').click();
    document.getElementById('auth-invite').value = inv;
  }
}

document.getElementById('auth-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const errBox = document.getElementById('auth-error');
  errBox.textContent = '';
  errBox.className = 'form-msg';

  const isSignup = document.querySelector('.auth-card .tab.active').dataset.tab === 'signup';
    const body = {
    username: document.getElementById('auth-username').value.trim(),
    password: document.getElementById('auth-password').value
  };
  if (isSignup) {
    const inv = document.getElementById('auth-invite').value.trim();
    if (inv) body.invite_code = inv;
    const affiliate = document.getElementById('auth-affiliate')?.value.trim();
    if (affiliate) body.affiliate_code = affiliate;
    body.tos_accepted = document.getElementById('tos-checkbox')?.checked || false;
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

  // 2FA required
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

  if (isSignup && res.data.recovery_phrase) {
    showRecoveryModal(res.data.recovery_phrase, res.data.recovery_key);
  } else {
    enterApp();
  }
});

// ==========  RECOVERY MODAL (after signup)  ==========
function showRecoveryModal(phrase, key) {
  const html = `
    <h3>🔐 <span class="accent">Save your recovery info</span></h3>
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

// ==========  FORGOT PASSWORD MODAL  ==========
function showRecoverModal() {
  const html = `
    <h3>🔓 <span class="accent">Reset your password</span></h3>
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

// ==========  CREDITS MODAL  ==========
function showCreditsModal() {
  const color = currentUser?.theme_color || '#00d9ff';
  const colorName = getColorName(color);
  showModal(`
    <div class="credits-box">
      <div class="credits-logo">CIPHER</div>
      <div class="credits-version">v1.1.0</div>
      <div class="credits-line">— A solo project by —</div>
      <div class="credits-name">STEPUNDRIK</div>
      <div class="credits-roles">Backend · Frontend · Design<br>Database · Deployment</div>
      <div class="credits-line">Made with love and too much ${colorName}.</div>
      <div class="credits-heart">Built with care.</div>
      <div class="credits-copy">© 2026 · All rights reserved</div>
    </div>
  `);
}

function getColorName(hex) {
  const map = {
    "#00d9ff": "cyan", "#ef4444": "red", "#f59e0b": "orange", "#eab308": "yellow",
    "#22c55e": "green", "#3b82f6": "blue", "#8b5cf6": "purple", "#ec4899": "pink",
    "#f43f5e": "rose", "#ffffff": "white", "#000000": "black", "#6b7280": "gray",
    "#6366f1": "indigo", "#14b8a6": "teal", "#f97316": "orange"
  };
  let closest = "cyan", minDist = Infinity;
  for (const [c, name] of Object.entries(map)) {
    const dr = parseInt(c.slice(1,3),16) - parseInt(hex.slice(1,3),16);
    const dg = parseInt(c.slice(3,5),16) - parseInt(hex.slice(3,5),16);
    const db = parseInt(c.slice(5,7),16) - parseInt(hex.slice(5,7),16);
    const dist = dr*dr + dg*dg + db*db;
    if (dist < minDist) { minDist = dist; closest = name; }
  }
  return closest;
}

// ==========  SESSION + ENTER APP  ==========
async function checkSession() {
  const res = await api('/api/me');
  if (res.data.poll_config) pollConfig = res.data.poll_config;
  if (res.data.user) {
    currentUser = res.data.user;
    enterApp();
  }
  loadAnnouncements();
}

function enterApp() {
  document.getElementById('auth-screen').classList.add('hidden');
  document.getElementById('app-screen').classList.remove('hidden');
  document.getElementById('my-username').textContent = currentUser.username;
  document.getElementById('empty-username').textContent = currentUser.username;

  const av = document.getElementById('me-avatar');
  av.textContent = currentUser.username[0].toUpperCase();
  av.style.background = `linear-gradient(135deg, ${currentUser.nickname_color || '#00d9ff'}, #6366f1)`;

  updateTheme(currentUser.theme_color || '#00d9ff');

  if (currentUser.is_admin || currentUser.is_owner) {
    document.getElementById('admin-btn').classList.remove('hidden');
  }

  loadConversations();
  startConvListPolling();
  loadAnnouncements();

  // Global paste handler for images in chat
  document.addEventListener('paste', handlePaste);
  // Drag & drop on messages
  const msgs = document.getElementById('messages');
  msgs.addEventListener('dragover', (e) => { e.preventDefault(); });
  msgs.addEventListener('drop', handleDrop);
  // Visibility change for polling speed
  document.addEventListener('visibilitychange', () => {
    if (currentConv) restartPollingWithNewInterval();
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
  const list = document.getElementById('conv-list');
  const convs = res.data.conversations || [];

  // Hash check to prevent flicker
  const h = JSON.stringify(convs.map(c => [c.id, c.updated_at, c.muted, c.last_message]));
  if (h === convListHash) return;
  convListHash = h;

  list.innerHTML = '';
  if (convs.length === 0) {
    list.innerHTML = '<div style="text-align:center; color:var(--text-mute); padding:20px; font-size:12px;">No conversations yet.<br>Start one with the + button.</div>';
    return;
  }

  convs.forEach(c => {
    const isGroup = c.is_group;
    const other = isGroup ? null : c.members.find(m => m.id !== currentUser.id);
    const title = isGroup ? (c.name || 'Group') : (other ? other.username : 'Chat');
    const color = isGroup ? '#6366f1' : (other ? other.nickname_color : '#00d9ff');
    const initial = title[0].toUpperCase();

    const div = document.createElement('div');
    div.className = 'conv-item' + (c.id === currentConv ? ' active' : '');
    div.dataset.cid = c.id;
    div.innerHTML = `
      <div class="avatar sm" style="background: linear-gradient(135deg, ${color}, var(--bg-5)); width:38px; height:38px; font-size:14px;">${esc(initial)}</div>
      <div class="conv-body">
        <div class="conv-top">
          <span class="conv-name">${esc(title)}${isGroup ? ' <span style="opacity:0.5;font-size:10px;">group</span>' : ''}</span>
          <span class="conv-time">${formatShortTime(c.last_time)}</span>
        </div>
        <div class="conv-preview">${esc(c.last_message || 'No messages yet')}</div>
      </div>
      ${c.muted ? '<span class="conv-mute-ico">🔕</span>' : ''}
    `;
    div.addEventListener('click', () => openConversation(c));
    list.appendChild(div);
  });
}

function startConvListPolling() {
  if (convListTimer) clearInterval(convListTimer);
  convListTimer = setInterval(loadConversations, 10000);
}

// ==========  OPEN CONVERSATION  ==========
function openConversation(conv) {
  currentConv = conv.id;
  currentConvMeta = conv;

  document.querySelectorAll('.conv-item').forEach(el => {
    el.classList.toggle('active', el.dataset.cid === conv.id);
  });

  document.getElementById('empty-state').classList.add('hidden');
  document.getElementById('chat-view').classList.remove('hidden');

  const isGroup = conv.is_group;
  const other = isGroup ? null : conv.members.find(m => m.id !== currentUser.id);
  const title = isGroup ? (conv.name || 'Group') : (other ? other.username : 'Chat');
  const color = isGroup ? '#6366f1' : (other ? other.nickname_color : '#00d9ff');

  document.getElementById('chat-title').textContent = title;
  const av = document.getElementById('chat-avatar');
  av.textContent = title[0].toUpperCase();
  av.style.background = `linear-gradient(135deg, ${color}, var(--bg-5))`;

  const sub = document.getElementById('chat-subtitle');
  sub.textContent = isGroup ? `${conv.members.length} members` : '';

  document.getElementById('group-info-btn').classList.toggle('hidden', !isGroup);

  // Mute button icon reflects state (we don't have separate icon, just visual color)
  const muteBtn = document.getElementById('mute-btn');
  muteBtn.style.color = conv.muted ? 'var(--warn)' : '';

  msgListHash = '';
  loadMessages();
  restartPollingWithNewInterval();
}

// ==========  POLLING WITH ADAPTIVE INTERVAL  ==========
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

// ==========  MESSAGES  ==========
async function loadMessages() {
  if (!currentConv) return;
  const res = await api(`/api/messages/${currentConv}`);
  if (!res.ok) return;

  const msgs = res.data.messages || [];
  const box = document.getElementById('messages');
  const wasAtBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 100;

  // Hash to avoid re-render if unchanged
  const h = JSON.stringify(msgs.map(m => [m.id, m.content, m.image_url, m.reactions, m.read_by, m.deleted]));
  if (h === msgListHash) {
    idleCycles++;
  } else {
    msgListHash = h;
    idleCycles = 0;
    renderMessages(msgs, box, wasAtBottom);
  }

  // Expiry warning banner
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
  box.innerHTML = '';
  let lastSender = null;
  let lastDate = null;
  let lastTime = 0;

  msgs.forEach((m, i) => {
    const when = new Date(m.created_at);
    const dayKey = when.toDateString();
    if (dayKey !== lastDate) {
      const div = document.createElement('div');
      div.className = 'date-divider';
      div.textContent = formatDayLabel(m.created_at);
      box.appendChild(div);
      lastDate = dayKey;
      lastSender = null;
      lastTime = 0;
    }

    const mine = m.sender_id === currentUser.id;
    const sameSender = m.sender_id === lastSender;
    const closeInTime = (when.getTime() - lastTime) < 5 * 60 * 1000;
    const grouped = sameSender && closeInTime;

    let group;
    if (grouped) {
      group = box.lastElementChild;
    } else {
      group = document.createElement('div');
      group.className = 'msg-group ' + (mine ? 'mine' : 'theirs');
      if (!mine) {
        const senderName = m.is_anonymous ? 'Anonymous' : (m.users?.username || 'Unknown');
        const color = m.users?.nickname_color || '#00d9ff';
        group.innerHTML = `<div class="msg-sender" style="color: ${esc(color)}">${esc(senderName)}</div>`;
      }
      box.appendChild(group);
    }

    // Build bubble
    let inner = '';
    if (m.image_url) {
      inner += `<img src="${esc(m.image_url)}" class="msg-img" alt="image" onclick="viewImage('${esc(m.image_url)}')">`;
    }
    if (m.content) {
      const cls = m.image_url ? 'msg-text' : '';
      inner += `<div class="${cls}">${esc(m.content)}</div>`;
    }
    const bubble = document.createElement('div');
    bubble.className = 'msg';
    bubble.innerHTML = inner;
    group.appendChild(bubble);

    // Meta (only on last message of group)
    const isLastOfGroup = (i === msgs.length - 1) || (msgs[i + 1] && (msgs[i + 1].sender_id !== m.sender_id || (new Date(msgs[i + 1].created_at) - when) >= 5 * 60 * 1000));
    if (isLastOfGroup) {
      const meta = document.createElement('div');
      meta.className = 'msg-meta';
      let metaHtml = `<span class="msg-time">${formatTime(m.created_at)}</span>`;
      metaHtml += ` <button class="react-btn" type="button" onclick="showEmojiPicker(event, '${m.id}')">＋</button>`;
      if (mine && m.read_by && m.read_by.filter(u => u !== currentUser.username).length > 0) {
        metaHtml += ` <span class="msg-seen" title="Seen">✓✓</span>`;
      }
      meta.innerHTML = metaHtml;
      group.appendChild(meta);
    }

    // Reactions
    if (m.reactions && m.reactions.length > 0) {
      const counts = {};
      m.reactions.forEach(r => {
        counts[r.emoji] = counts[r.emoji] || { count: 0, mine: false };
        counts[r.emoji].count++;
        if (r.user_id === currentUser.id) counts[r.emoji].mine = true;
      });
      const rw = document.createElement('div');
      rw.className = 'msg-reactions';
      rw.innerHTML = Object.entries(counts).map(([em, d]) =>
        `<span class="reaction ${d.mine ? 'mine' : ''}" onclick="toggleReaction('${m.id}', '${esc(em)}')">${esc(em)} ${d.count}</span>`
      ).join('');
      group.appendChild(rw);
    }

    lastSender = m.sender_id;
    lastTime = when.getTime();
  });

  if (keepScroll) box.scrollTop = box.scrollHeight;
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

// ==========  EMOJI PICKER (viewport-safe)  ==========
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

  // Position with viewport clamping
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

  const body = { content };
  if (pendingImage) body.image_data = pendingImage;

  input.value = '';
  const imgToSend = pendingImage;
  clearImagePreview();

  const res = await api(`/api/messages/${currentConv}`, { method: 'POST', body });

  if (res.data.blocked) {
    toast(res.data.error, 'error', 6000);
    return;
  }
  if (!res.ok) {
    toast(res.data.error || 'Failed to send', 'error');
    // Restore input
    input.value = content;
    if (imgToSend) pendingImage = imgToSend;
    return;
  }

  if (res.data.warning) {
    toast(res.data.warning, 'warn', 6000);
  }
  if (res.data.throttle_delay) {
    toast(`You are throttled. Messages send with a delay.`, 'warn', 3000);
  }

  msgListHash = '';
  loadMessages();
});

// ==========  TYPING INDICATOR  ==========
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

function compressImage(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = (e) => {
      const img = new Image();
      img.onload = () => {
        const MAX = 1200;
        let w = img.width, h = img.height;
        if (w > MAX || h > MAX) {
          if (w > h) { h = h * (MAX / w); w = MAX; }
          else { w = w * (MAX / h); h = MAX; }
        }
        const canvas = document.createElement('canvas');
        canvas.width = w; canvas.height = h;
        canvas.getContext('2d').drawImage(img, 0, 0, w, h);
        resolve(canvas.toDataURL('image/jpeg', 0.7));
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
        <span style="color: ${esc(m.nickname_color)}; font-weight: 500;">${esc(m.username)}</span>
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
    // Reload conversation
    const cRes = await api('/api/conversations');
    const updated = (cRes.data.conversations || []).find(c => c.id === currentConv);
    if (updated) { currentConvMeta = updated; showGroupInfo(); }
  } else toast(res.data.error, 'error');
}

async function removeGroupMember(uid, username) {
  if (!confirm(`Remove ${username} from group?`)) return;
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
  // Find and open it
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
      box.innerHTML = results.map(r => `
        <div class="search-result" onclick='openFromSearch("${r.conversation_id}")'>
          <div class="search-result-meta">
            <span class="search-result-user">${esc(r.users?.username || 'unknown')}</span>
            <span class="search-result-time">${new Date(r.created_at).toLocaleString()}</span>
          </div>
          <div class="search-result-content">${esc((r.content || '').slice(0, 150))}</div>
        </div>
      `).join('');
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
document.getElementById('invites-btn').addEventListener('click', showInvitesModal);

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

// ==========  SETTINGS  ==========
document.getElementById('settings-btn').addEventListener('click', showSettingsModal);

function showSettingsModal() {
  const u = currentUser;
  showModal(`
    <h3>⚙️ <span class="accent">Your settings</span></h3>

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
        <div class="setting-desc">Hide your name on new messages</div>
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
    <button class="btn primary" style="margin-top: 12px;" onclick="saveSettings()">Save changes</button>

    <h4>Security</h4>
    <button class="btn secondary" onclick="showChangePasswordModal()" style="margin-bottom: 8px;">Change password</button>
    ${u.totp_enabled
      ? `<button class="btn secondary" onclick="showDisable2FAModal()">Disable 2FA</button>`
      : `<button class="btn secondary" onclick="showEnable2FAModal()">Enable 2FA (Authenticator App)</button>`
    }

    <h4>Danger zone</h4>
    <button class="btn danger" onclick="showDeleteAccountModal()">Delete my account</button>
  `);
}

async function saveSettings() {
  const body = {
    nickname_color: document.getElementById('s-nick').value,
    theme_color: document.getElementById('s-theme').value,
    anonymous_mode: document.getElementById('s-anon').checked,
    keep_all_forever: document.getElementById('s-keep').checked,
    notify_before_delete: document.getElementById('s-notify').checked
  };
  const res = await api('/api/profile', { method: 'POST', body });
  if (!res.ok) return toast('Save failed', 'error');
  Object.assign(currentUser, body);
  updateTheme(body.theme_color);
  const av = document.getElementById('me-avatar');
  av.style.background = `linear-gradient(135deg, ${body.nickname_color}, #6366f1)`;
  toast('Settings saved', 'success');
  closeModal();
  convListHash = '';
  loadConversations();
}

function showChangePasswordModal() {
  showModal(`
    <h3>🔑 <span class="accent">Change password</span></h3>
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
    <h3>🔐 <span class="accent">Enable 2FA</span></h3>
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
      <li>All your messages (marked as deleted)</li>
      <li>All your conversations you're alone in</li>
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

// ==========  ADMIN PANEL  ==========
document.getElementById('admin-btn').addEventListener('click', () => {
  showModal(`
    <h3>👑 <span class="accent">Admin panel</span></h3>
    <div class="admin-tabs">
      <button class="admin-tab active" onclick="switchAdmin(this,'stats')">Stats</button>
      <button class="admin-tab" onclick="switchAdmin(this,'users')">Users</button>
      <button class="admin-tab" onclick="switchAdmin(this,'bans')">IP Bans</button>
      <button class="admin-tab" onclick="switchAdmin(this,'ann')">Announcements</button>
      <button class="admin-tab" onclick="switchAdmin(this,'set')">Settings</button>
      <button class="admin-tab" onclick="switchAdmin(this,'audit')">Audit log</button>
      <button class="admin-tab" onclick="switchAdmin(this,'spam')">Spam</button>
    </div>
    <div id="admin-body"></div>
  `, true);
  loadAdminStats();
});

function switchAdmin(btn, tab) {
  document.querySelectorAll('.admin-tab').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  ({
    stats: loadAdminStats,
    users: loadAdminUsers,
    bans: loadAdminBans,
    ann: loadAdminAnn,
    set: loadAdminSettings,
    audit: loadAdminAudit,
    spam: loadAdminSpam
  }[tab] || loadAdminStats)();
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

async function loadAdminUsers() {
  const body = document.getElementById('admin-body');
  body.innerHTML = 'Loading...';
  const res = await api('/api/admin/users');
  const users = res.data.users || [];
  body.innerHTML = `
    <table>
      <thead><tr><th>User</th><th>IP</th><th>Last seen</th><th>Actions</th></tr></thead>
      <tbody>${users.map(u => `
        <tr>
          <td>
            <span style="color: ${esc(u.nickname_color)}; font-weight: 500;">${esc(u.username)}</span>
            ${u.is_owner ? '<span class="badge owner">Owner</span>' : ''}
            ${u.is_admin && !u.is_owner ? '<span class="badge admin">Admin</span>' : ''}
            ${u.is_immune ? '<span class="badge info">Immune</span>' : ''}
            ${u.suspended ? '<span class="badge suspended">Suspended</span>' : ''}
            ${u.totp_enabled ? '<span class="badge info">2FA</span>' : ''}
            ${(u.throttle_level || 0) > 0 ? `<span class="badge warn">Throttle L${u.throttle_level}</span>` : ''}
            ${(u.spam_warnings || 0) > 0 ? `<span class="badge critical">${u.spam_warnings} warns</span>` : ''}
          </td>
          <td><code>${esc(u.last_ip || '-')}</code></td>
          <td style="font-size: 11px; color: var(--text-mute);">${u.last_seen ? new Date(u.last_seen).toLocaleDateString() : '-'}</td>
          <td>
            ${!u.is_immune ? `
              <button class="btn-mini" onclick="adminToggle('${u.id}','suspended',${!u.suspended})">${u.suspended ? 'Unsuspend' : 'Suspend'}</button>
              <button class="btn-mini" onclick="adminToggle('${u.id}','is_admin',${!u.is_admin})">${u.is_admin ? '-Admin' : '+Admin'}</button>
              <button class="btn-mini warn" onclick="adminPunish('${u.id}','${esc(u.username)}')">Punish</button>
              <button class="btn-mini" onclick="adminResetPw('${u.id}')">Reset PW</button>
              <button class="btn-mini danger" onclick="adminBanIP('${esc(u.last_ip || '')}')">Ban IP</button>
            ` : '<span style="color: var(--text-mute); font-size: 11px;">Immune (protected)</span>'}
          </td>
        </tr>
      `).join('')}</tbody>
    </table>
  `;
}

async function adminToggle(uid, field, val) {
  const res = await api(`/api/admin/user/${uid}`, { method: 'POST', body: { [field]: val } });
  if (!res.ok) return toast(res.data.error, 'error');
  toast('Updated', 'success');
  loadAdminUsers();
}

async function adminResetPw(uid) {
  if (!confirm('Reset password for this user?')) return;
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

async function adminBanIP(ip) {
  if (!ip || ip === '-') return toast('No IP available', 'error');
  const reason = prompt(`Ban IP ${ip}? Reason:`) || '';
  const res = await api('/api/admin/ban', { method: 'POST', body: { ip, reason } });
  if (!res.ok) return toast(res.data.error, 'error');
  toast('IP banned', 'success');
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

async function loadAdminSettings() {
  const body = document.getElementById('admin-body');
  const res = await api('/api/admin/settings');
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
    <p>Recent spam detection events. Users get warnings 1-5 before permanent IP ban.</p>
    <table>
      <thead><tr><th>User</th><th>Warning #</th><th>Reason</th><th>IP</th><th>When</th></tr></thead>
      <tbody>${events.map(e => `
        <tr>
          <td>${esc(e.users?.username || '?')}</td>
          <td><span class="badge ${e.warning_number >= 4 ? 'critical' : 'warn'}">${e.warning_number}</span></td>
          <td>${esc(e.trigger_reason || '-')}</td>
          <td><code>${esc(e.ip_address || '-')}</code></td>
          <td style="font-size: 11px; color: var(--text-mute);">${new Date(e.created_at).toLocaleString()}</td>
        </tr>
      `).join('') || '<tr class="empty-row"><td colspan="5">No spam events</td></tr>'}</tbody>
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

// ==========  BOOTSTRAP  ==========
initAuthTabs();
checkSession();

// Refresh announcements periodically
setInterval(loadAnnouncements, 60000);
ents, 60000);
