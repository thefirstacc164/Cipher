// ============ STATE ============
let currentUser = null;
let currentConv = null;
let pollTimer = null;
let typingTimer = null;
let pendingImage = null;
let dismissedAnnouncements = JSON.parse(localStorage.getItem('dismissedAnns') || '[]');

// ============ AUTH TAB ============
document.querySelectorAll('.tab').forEach(t => {
  t.onclick = () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    const isSignup = t.dataset.tab === 'signup';
    document.getElementById('auth-invite').style.display = isSignup ? 'block' : 'none';
    document.getElementById('auth-submit').textContent = isSignup ? 'Sign up' : 'Log in';
  };
});

// Pre-fill invite from URL
const urlParams = new URLSearchParams(window.location.search);
const inviteFromUrl = urlParams.get('invite');
if (inviteFromUrl) {
  document.querySelector('[data-tab="signup"]').click();
  document.getElementById('auth-invite').value = inviteFromUrl;
}

// ============ AUTH SUBMIT ============
document.getElementById('auth-form').onsubmit = async (e) => {
  e.preventDefault();
  const username = document.getElementById('auth-username').value;
  const password = document.getElementById('auth-password').value;
  const invite = document.getElementById('auth-invite').value;
  const isSignup = document.querySelector('.tab.active').dataset.tab === 'signup';
  const endpoint = isSignup ? '/api/signup' : '/api/login';
  const body = { username, password };
  if (isSignup && invite) body.invite_code = invite;

  const r = await fetch(endpoint, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  const j = await r.json();
  if (!r.ok) {
    document.getElementById('auth-error').textContent = j.error || 'Error';
    return;
  }
  currentUser = j.user;

  if (isSignup && j.recovery_phrase && j.recovery_key) {
    showRecoveryDownload(j.recovery_phrase, j.recovery_key, () => enterApp());
  } else {
    enterApp();
  }
};

// ============ RECOVERY DOWNLOAD (After Signup) ============
function showRecoveryDownload(phrase, key, onContinue) {
  showModal(`
    <h3>🔐 Save Your Recovery Info!</h3>
    <p style="color: var(--warn); margin-bottom: 16px;">⚠️ Save BOTH of these NOW. You cannot recover them later.</p>

    <div class="recovery-box">
      <div class="recovery-title">🗝️ Recovery Phrase (12 words)</div>
      <p style="color:var(--text-dim); font-size:12px;">Write it down or save it. Used to reset your password.</p>
      <div class="recovery-content" id="phrase-content">${phrase}</div>
      <button class="mini-btn" onclick="copyText('${phrase}')">Copy Phrase</button>
    </div>

    <div class="recovery-box">
      <div class="recovery-title">📄 Recovery Key File</div>
      <p style="color:var(--text-dim); font-size:12px;">Download this file. Keep it safe. Use it as backup to reset password.</p>
      <button class="download-btn" onclick="downloadRecoveryFile('${currentUser.username}', '${key}')">⬇️ Download cipher-recovery.key</button>
    </div>

    <label style="display:flex; align-items:center; gap:8px; margin:16px 0; cursor:pointer;">
      <input type="checkbox" id="confirm-saved" style="width:auto; margin:0;">
      <span>I have saved both my recovery phrase AND the recovery file</span>
    </label>
    <button onclick="continueAfterRecovery()" id="continue-btn" disabled style="opacity:0.5;">Continue</button>
  `);
  setTimeout(() => {
    document.getElementById('confirm-saved').onchange = (e) => {
      const btn = document.getElementById('continue-btn');
      btn.disabled = !e.target.checked;
      btn.style.opacity = e.target.checked ? '1' : '0.5';
    };
  }, 50);
  window._recoveryContinue = onContinue;
}

function continueAfterRecovery() {
  closeModal();
  if (window._recoveryContinue) window._recoveryContinue();
}

function downloadRecoveryFile(username, key) {
  const content = `============================================
   CIPHER RECOVERY KEY FILE
   DO NOT SHARE. DO NOT EDIT.
============================================

Username: ${username}
Generated: ${new Date().toISOString()}

RECOVERY KEY:
${key}

============================================
HOW TO USE:
Go to Cipher login page → click "Forgot password?"
→ choose "Recovery key file" → paste the key above.
============================================
`;
  const blob = new Blob([content], { type: 'text/plain' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `cipher-recovery-${username}.key`;
  a.click();
  URL.revokeObjectURL(url);
}

function copyText(text) {
  navigator.clipboard.writeText(text);
  alert('Copied!');
}

// ============ FORGOT PASSWORD ============
function showRecover() {
  showModal(`
    <h3>🔓 Reset Password</h3>
    <p style="color:var(--text-dim); margin-bottom:16px;">Choose your recovery method:</p>
    <div class="tabs">
      <button class="tab active" onclick="switchRecover(this,'phrase')">12-Word Phrase</button>
      <button class="tab" onclick="switchRecover(this,'key')">Recovery File</button>
    </div>
    <div id="recover-form">${recoverFormHtml('phrase')}</div>
  `);
}

function switchRecover(btn, method) {
  document.querySelectorAll('.modal .tab').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('recover-form').innerHTML = recoverFormHtml(method);
}

function recoverFormHtml(method) {
  if (method === 'phrase') {
    return `
      <input type="text" id="rec-username" placeholder="Username">
      <textarea id="rec-phrase" placeholder="Your 12-word recovery phrase" rows="3" style="font-family:monospace"></textarea>
      <input type="password" id="rec-newpass" placeholder="New password (6+ chars)">
      <button onclick="doRecover('phrase')">Reset Password</button>
      <p id="rec-msg" class="error"></p>
    `;
  }
  return `
    <input type="text" id="rec-username" placeholder="Username">
    <p style="color:var(--text-dim); font-size:12px; margin-bottom:8px;">Upload your recovery key file:</p>
    <input type="file" id="rec-file" accept=".key,.txt" onchange="readKeyFile(event)">
    <textarea id="rec-key" placeholder="Or paste your recovery key here" rows="2" style="font-family:monospace"></textarea>
    <input type="password" id="rec-newpass" placeholder="New password (6+ chars)">
    <button onclick="doRecover('key')">Reset Password</button>
    <p id="rec-msg" class="error"></p>
  `;
}

function readKeyFile(e) {
  const file = e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = (ev) => {
    const text = ev.target.result;
    const match = text.match(/RECOVERY KEY:\s*\n(.+)/i);
    if (match) {
      document.getElementById('rec-key').value = match[1].trim();
    } else {
      document.getElementById('rec-key').value = text.trim();
    }
  };
  reader.readAsText(file);
}

async function doRecover(method) {
  const username = document.getElementById('rec-username').value.trim();
  const newpass = document.getElementById('rec-newpass').value;
  const msg = document.getElementById('rec-msg');
  const body = { username, new_password: newpass };
  if (method === 'phrase') body.phrase = document.getElementById('rec-phrase').value.trim();
  else body.key = document.getElementById('rec-key').value.trim();

  const r = await fetch(`/api/recover/${method}`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify(body)
  });
  const j = await r.json();
  if (!r.ok) { msg.textContent = j.error; return; }
  msg.className = 'success';
  msg.textContent = '✅ Password reset! You can log in now.';
  setTimeout(closeModal, 2000);
}

// ============ LOGOUT ============
document.getElementById('logout-btn').onclick = async () => {
  await fetch('/api/logout', {method: 'POST'});
  location.reload();
};

// ============ SESSION CHECK ============
async function checkSession() {
  const r = await fetch('/api/me');
  const j = await r.json();
  if (j.user) {
    currentUser = j.user;
    enterApp();
  }
  loadAnnouncements();
}

// ============ ENTER APP ============
function enterApp() {
  document.getElementById('auth-screen').classList.add('hidden');
  document.getElementById('app-screen').classList.remove('hidden');
  document.getElementById('my-username').textContent = currentUser.username;
  if (currentUser.theme_color) applyThemeColor(currentUser.theme_color);
  if (currentUser.is_admin || currentUser.is_owner) {
    document.getElementById('admin-btn').classList.remove('hidden');
  }
  loadConversations();
  loadAnnouncements();
}

function applyThemeColor(hex) {
  document.documentElement.style.setProperty('--accent', hex);
}

// ============ ANNOUNCEMENTS ============
async function loadAnnouncements() {
  const r = await fetch('/api/announcements');
  const j = await r.json();
  const bar = document.getElementById('announcement-bar');
  const ann = j.announcements.find(a => !dismissedAnnouncements.includes(a.id));
  if (!ann) { bar.classList.add('hidden'); return; }
  bar.className = 'announcement-bar ' + (ann.priority || 'info');
  bar.innerHTML = `<button class="close-ann" onclick="dismissAnn('${ann.id}')">×</button><b>${escapeHtml(ann.title)}</b> — ${escapeHtml(ann.content)}`;
  bar.classList.remove('hidden');
}

function dismissAnn(id) {
  dismissedAnnouncements.push(id);
  localStorage.setItem('dismissedAnns', JSON.stringify(dismissedAnnouncements));
  document.getElementById('announcement-bar').classList.add('hidden');
}

// ============ CONVERSATIONS ============
async function loadConversations() {
  const r = await fetch('/api/conversations');
  const j = await r.json();
  const list = document.getElementById('conv-list');
  list.innerHTML = '';
  j.conversations.forEach(c => {
    const others = c.members.filter(m => m.id !== currentUser.id);
    const name = others.map(m => m.username).join(', ') || 'Empty chat';
    const div = document.createElement('div');
    div.className = 'conv-item' + (c.muted ? ' conv-muted' : '');
    div.innerHTML = `<div class="conv-name">${escapeHtml(name)}</div>`;
    div.onclick = (e) => openConversation(c.id, name, e.currentTarget);
    list.appendChild(div);
  });
}

async function openConversation(id, title, el) {
  currentConv = id;
  document.querySelectorAll('.conv-item').forEach(x => x.classList.remove('active'));
  if (el) el.classList.add('active');
  document.getElementById('empty-state').classList.add('hidden');
  document.getElementById('chat-view').classList.remove('hidden');
  document.getElementById('chat-title').textContent = title || 'Chat';
  await loadMessages();
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => { loadMessages(); pollTyping(); }, 3000);
}

async function loadMessages() {
  if (!currentConv) return;
  const r = await fetch(`/api/messages/${currentConv}`);
  const j = await r.json();
  const box = document.getElementById('messages');
  const wasAtBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  box.innerHTML = '';

  j.messages.forEach(m => {
    const mine = m.sender_id === currentUser.id;
    const wrap = document.createElement('div');
    wrap.className = 'msg-wrap ' + (mine ? 'mine' : 'theirs');

    const sender = m.is_anonymous ? '👤 Anonymous' : (m.users?.username || 'unknown');
    const color = m.users?.nickname_color || '#00d9ff';
    const time = new Date(m.created_at).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});

    let inner = '';
    if (!mine) inner += `<div class="msg-sender" style="color:${color}">${escapeHtml(sender)}</div>`;
    inner += `<div class="msg">`;
    if (m.image_url) inner += `<img src="${m.image_url}" class="msg-img" onclick="viewImage('${m.image_url}')">`;
    if (m.content) inner += `<div>${escapeHtml(m.content)}</div>`;
    inner += `</div>`;

    // Reactions
    if (m.reactions && m.reactions.length) {
      const grouped = {};
      m.reactions.forEach(r => {
        grouped[r.emoji] = grouped[r.emoji] || { count: 0, mine: false };
        grouped[r.emoji].count++;
        if (r.user === currentUser.username) grouped[r.emoji].mine = true;
      });
      inner += `<div class="msg-reactions">`;
      Object.entries(grouped).forEach(([em, d]) => {
        inner += `<span class="reaction ${d.mine ? 'mine' : ''}" onclick="toggleReact('${m.id}','${em}')">${em} ${d.count}</span>`;
      });
      inner += `</div>`;
    }

    inner += `<div class="msg-meta">${time} <button class="react-btn" onclick="showEmojiPicker(event,'${m.id}')">😀+</button></div>`;

    // Seen indicators (only for my messages)
    if (mine && m.read_by && m.read_by.length) {
      const seenBy = m.read_by.filter(u => u !== currentUser.username);
      if (seenBy.length) inner += `<div class="seen-by">✓ Seen by ${seenBy.join(', ')}</div>`;
    }

    wrap.innerHTML = inner;
    box.appendChild(wrap);
  });
  if (wasAtBottom) box.scrollTop = box.scrollHeight;
}

// ============ SEND MESSAGE + TYPING ============
document.getElementById('msg-form').onsubmit = async (e) => {
  e.preventDefault();
  const input = document.getElementById('msg-input');
  const content = input.value.trim();
  if ((!content && !pendingImage) || !currentConv) return;
  const body = { content };
  if (pendingImage) body.image_data = pendingImage;
  input.value = '';
  const wasImage = pendingImage;
  pendingImage = null;
  document.getElementById('image-preview').classList.add('hidden');
  await fetch(`/api/messages/${currentConv}`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  loadMessages();
};

document.getElementById('msg-input').oninput = () => {
  if (!currentConv) return;
  if (typingTimer) return;
  fetch(`/api/typing/${currentConv}`, {method:'POST'});
  typingTimer = setTimeout(() => { typingTimer = null; }, 3000);
};

async function pollTyping() {
  if (!currentConv) return;
  const r = await fetch(`/api/typing/${currentConv}`);
  const j = await r.json();
  const ind = document.getElementById('typing-indicator');
  if (j.typing.length === 0) { ind.classList.add('hidden'); return; }
  ind.classList.remove('hidden');
  ind.textContent = j.typing.join(', ') + (j.typing.length === 1 ? ' is typing...' : ' are typing...');
}

// ============ IMAGE UPLOAD ============
async function handleImageSelect(e) {
  const file = e.target.files[0];
  if (!file) return;
  if (!file.type.startsWith('image/')) { alert('Only images allowed'); return; }
  const compressed = await compressImage(file);
  pendingImage = compressed;
  const preview = document.getElementById('image-preview');
  preview.innerHTML = `<img src="${compressed}"><button onclick="cancelImage()">Cancel</button>`;
  preview.classList.remove('hidden');
  e.target.value = '';
}

function cancelImage() {
  pendingImage = null;
  document.getElementById('image-preview').classList.add('hidden');
}

function compressImage(file) {
  return new Promise((resolve) => {
    const img = new Image();
    const reader = new FileReader();
    reader.onload = (e) => { img.src = e.target.result; };
    img.onload = () => {
      const maxDim = 1200;
      let w = img.width, h = img.height;
      if (w > maxDim || h > maxDim) {
        if (w > h) { h = h * maxDim / w; w = maxDim; }
        else { w = w * maxDim / h; h = maxDim; }
      }
      const canvas = document.createElement('canvas');
      canvas.width = w; canvas.height = h;
      canvas.getContext('2d').drawImage(img, 0, 0, w, h);
      resolve(canvas.toDataURL('image/jpeg', 0.75));
    };
    reader.readAsDataURL(file);
  });
}

function viewImage(url) {
  document.getElementById('img-viewer-img').src = url;
  document.getElementById('img-viewer').classList.remove('hidden');
}

// ============ REACTIONS ============
function showEmojiPicker(e, msgId) {
  e.stopPropagation();
  document.querySelectorAll('.emoji-picker').forEach(x => x.remove());
  const picker = document.createElement('div');
  picker.className = 'emoji-picker';
  ['👍','❤️','😂','😮','😢','🔥','💯','👀'].forEach(em => {
    const b = document.createElement('button');
    b.textContent = em;
    b.onclick = (ev) => { ev.stopPropagation(); toggleReact(msgId, em); picker.remove(); };
    picker.appendChild(b);
  });
  document.body.appendChild(picker);
  const rect = e.target.getBoundingClientRect();
  picker.style.top = (rect.top - 50) + 'px';
  picker.style.left = rect.left + 'px';
  setTimeout(() => document.addEventListener('click', () => picker.remove(), {once: true}), 100);
}

async function toggleReact(msgId, emoji) {
  await fetch(`/api/messages/${msgId}/react`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({emoji})
  });
  loadMessages();
}

// ============ CONV ACTIONS ============
async function muteCurrent() {
  if (!currentConv) return;
  await fetch(`/api/conversations/${currentConv}/mute`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({muted: true})
  });
  loadConversations();
}

async function leaveCurrent() {
  if (!currentConv) return;
  if (!confirm('Leave this conversation? If everyone leaves, it will be deleted.')) return;
  await fetch(`/api/conversations/${currentConv}/leave`, {method: 'POST'});
  currentConv = null;
  document.getElementById('chat-view').classList.add('hidden');
  document.getElementById('empty-state').classList.remove('hidden');
  loadConversations();
}

function exportCurrent() {
  if (!currentConv) return;
  window.location.href = `/api/conversations/${currentConv}/export`;
}

// ============ NEW CHAT ============
document.getElementById('new-chat-btn').onclick = () => {
  showModal(`
    <h3>Start a New Chat</h3>
    <input type="text" id="new-chat-user" placeholder="Enter username">
    <button onclick="createChat()">Start Chat</button>
    <p id="new-chat-err" class="error"></p>
  `);
};

async function createChat() {
  const username = document.getElementById('new-chat-user').value.trim();
  if (!username) return;
  const r = await fetch('/api/conversations/new', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({username})
  });
  const j = await r.json();
  if (!r.ok) { document.getElementById('new-chat-err').textContent = j.error; return; }
  closeModal();
  await loadConversations();
  openConversation(j.conversation_id, username);
}

// ============ SEARCH ============
document.getElementById('search-btn').onclick = () => {
  showModal(`
    <h3>🔍 Search Messages</h3>
    <input type="text" id="search-input" placeholder="Search..." oninput="doSearch()">
    <div id="search-results" style="margin-top:16px;"></div>
  `);
};

let searchTimer;
async function doSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(async () => {
    const q = document.getElementById('search-input').value.trim();
    const box = document.getElementById('search-results');
    if (q.length < 2) { box.innerHTML = ''; return; }
    const r = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
    const j = await r.json();
    if (!j.results.length) { box.innerHTML = '<p style="color:var(--text-dim); text-align:center;">No results</p>'; return; }
    box.innerHTML = j.results.map(m => `
      <div style="padding:10px; border-bottom:1px solid var(--border); cursor:pointer" onclick="closeModal(); openConversation('${m.conversation_id}','Chat')">
        <div style="color:var(--accent); font-size:12px;">${escapeHtml(m.users?.username || 'unknown')} · ${new Date(m.created_at).toLocaleString()}</div>
        <div>${escapeHtml((m.content || '').substring(0,120))}</div>
      </div>
    `).join('');
  }, 300);
}

// ============ SETTINGS ============
document.getElementById('settings-btn').onclick = () => {
  showModal(`
    <h3>⚙️ Your Settings</h3>

    <h4>Appearance</h4>
    <div class="color-row">
      <label>Nickname color</label>
      <input type="color" id="s-nick-color" value="${currentUser.nickname_color || '#00d9ff'}">
    </div>
    <div class="color-row">
      <label>Theme accent color</label>
      <input type="color" id="s-theme-color" value="${currentUser.theme_color || '#00d9ff'}">
    </div>

    <h4>Privacy</h4>
    <div class="setting-row">
      <div>
        <div class="setting-label">Anonymous mode</div>
        <div class="setting-desc">Hide your username on new messages</div>
      </div>
      <input type="checkbox" id="s-anon" ${currentUser.anonymous_mode ? 'checked' : ''}>
    </div>
    <div class="setting-row">
      <div>
        <div class="setting-label">Keep all my messages forever</div>
        <div class="setting-desc">Disable auto-delete for your messages</div>
      </div>
      <input type="checkbox" id="s-keep" ${currentUser.keep_all_forever ? 'checked' : ''}>
    </div>
    <div class="setting-row">
      <div>
        <div class="setting-label">Notify before delete</div>
        <div class="setting-desc">Warn 48h before messages auto-delete</div>
      </div>
      <input type="checkbox" id="s-notify" ${currentUser.notify_before_delete ? 'checked' : ''}>
    </div>
    <button onclick="saveSettings()" style="margin-top:16px;">Save Settings</button>

    <h4>Security</h4>
    <button class="secondary-btn" onclick="showChangePassword()">Change Password</button>
    <button class="mini-btn danger" style="width:100%; margin-top:8px;" onclick="showPanic()">💥 Panic Button</button>
  `);
};

async function saveSettings() {
  const body = {
    nickname_color: document.getElementById('s-nick-color').value,
    theme_color: document.getElementById('s-theme-color').value,
    anonymous_mode: document.getElementById('s-anon').checked,
    keep_all_forever: document.getElementById('s-keep').checked,
    notify_before_delete: document.getElementById('s-notify').checked
  };
  await fetch('/api/profile', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify(body)
  });
  Object.assign(currentUser, body);
  applyThemeColor(body.theme_color);
  alert('Saved!');
  closeModal();
  loadConversations();
}

function showChangePassword() {
  showModal(`
    <h3>🔑 Change Password</h3>
    <input type="password" id="cp-old" placeholder="Current password">
    <input type="password" id="cp-new" placeholder="New password (6+ chars)">
    <button onclick="doChangePass()">Change Password</button>
    <p id="cp-msg" class="error"></p>
  `);
}

async function doChangePass() {
  const old_password = document.getElementById('cp-old').value;
  const new_password = document.getElementById('cp-new').value;
  const r = await fetch('/api/change_password', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({old_password, new_password})
  });
  const j = await r.json();
  if (!r.ok) { document.getElementById('cp-msg').textContent = j.error; return; }
  alert('Password changed!');
  closeModal();
}

function showPanic() {
  showModal(`
    <h3 style="color:var(--danger)">💥 Panic Button</h3>
    <p>This will <b>PERMANENTLY DELETE</b>:</p>
    <ul style="margin:10px 0 16px 20px; color:var(--text-dim)">
      <li>Your entire account</li>
      <li>All your messages</li>
      <li>All your conversations</li>
    </ul>
    <p style="color:var(--danger); margin-bottom:16px;">This cannot be undone!</p>
    <input type="text" id="panic-confirm" placeholder='Type "DELETE ME" to confirm'>
    <button class="mini-btn danger" style="width:100%" onclick="doPanic()">💥 Wipe My Account</button>
  `);
}

async function doPanic() {
  if (document.getElementById('panic-confirm').value !== 'DELETE ME') {
    alert('Confirmation text does not match');
    return;
  }
  await fetch('/api/panic', {method: 'POST'});
  alert('Account wiped. Goodbye.');
  location.reload();
}

// ============ INVITES ============
document.getElementById('invites-btn').onclick = async () => {
  const r = await fetch('/api/invites');
  const j = await r.json();
  const rows = j.invites.map(i => `
    <tr>
      <td><code>${i.code}</code></td>
      <td>${i.uses_count}${i.max_uses ? '/'+i.max_uses : ''}</td>
      <td>${i.revoked ? '❌' : '✅'}</td>
      <td>
        <button class="mini-btn" onclick="copyInvite('${i.code}')">Copy</button>
        ${!i.revoked ? `<button class="mini-btn danger" onclick="revokeInvite('${i.id}')">Revoke</button>` : ''}
      </td>
    </tr>
  `).join('');
  showModal(`
    <h3>🎟️ Your Invite Links</h3>
    <div style="display:flex; gap:8px; margin-bottom:16px;">
      <input type="number" id="inv-uses" placeholder="Max uses (blank=∞)" style="margin:0">
      <input type="number" id="inv-hours" placeholder="Hours until expiry" style="margin:0">
      <button style="width:auto; margin:0;" onclick="makeInvite()">Create</button>
    </div>
    <table>
      <thead><tr><th>Code</th><th>Uses</th><th>Status</th><th>Actions</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="4" style="text-align:center; color:var(--text-dim); padding:20px;">No invites yet</td></tr>'}</tbody>
    </table>
  `);
};

async function makeInvite() {
  const max_uses = parseInt(document.getElementById('inv-uses').value) || null;
  const expires_hours = parseInt(document.getElementById('inv-hours').value) || null;
  const r = await fetch('/api/invites', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({max_uses, expires_hours})
  });
  const j = await r.json();
  if (!r.ok) { alert(j.error); return; }
  document.getElementById('invites-btn').click();
}

function copyInvite(code) {
  const url = window.location.origin + '/?invite=' + code;
  navigator.clipboard.writeText(url);
  alert('Copied: ' + url);
}

async function revokeInvite(id) {
  await fetch(`/api/invites/${id}/revoke`, {method: 'POST'});
  document.getElementById('invites-btn').click();
}

// ============ ADMIN PANEL ============
document.getElementById('admin-btn').onclick = () => {
  showModal(`
    <h3>👑 Admin Panel</h3>
    <div class="admin-tabs">
      <button class="admin-tab active" onclick="switchAdminTab(this,'stats')">📊 Stats</button>
      <button class="admin-tab" onclick="switchAdminTab(this,'users')">👥 Users</button>
      <button class="admin-tab" onclick="switchAdminTab(this,'bans')">🔨 Bans</button>
      <button class="admin-tab" onclick="switchAdminTab(this,'ann')">📢 Announcements</button>
      <button class="admin-tab" onclick="switchAdminTab(this,'settings')">⚙️ Settings</button>
      <button class="admin-tab" onclick="switchAdminTab(this,'audit')">📜 Audit</button>
    </div>
    <div id="admin-stats" class="admin-section active"></div>
    <div id="admin-users" class="admin-section"></div>
    <div id="admin-bans" class="admin-section"></div>
    <div id="admin-ann" class="admin-section"></div>
    <div id="admin-settings" class="admin-section"></div>
    <div id="admin-audit" class="admin-section"></div>
  `);
  loadAdminStats();
};

function switchAdminTab(btn, tab) {
  document.querySelectorAll('.admin-tab').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('.admin-section').forEach(s => s.classList.remove('active'));
  document.getElementById('admin-' + tab).classList.add('active');
  if (tab === 'stats') loadAdminStats();
  if (tab === 'users') loadAdminUsers();
  if (tab === 'bans') loadAdminBans();
  if (tab === 'ann') loadAdminAnn();
  if (tab === 'settings') loadAdminSettings();
  if (tab === 'audit') loadAdminAudit();
}

async function loadAdminStats() {
  const r = await fetch('/api/admin/stats');
  const s = await r.json();
  document.getElementById('admin-stats').innerHTML = `
    <div class="stats-grid">
      <div class="stat-card"><div class="stat-num">${s.users}</div><div class="stat-label">Total Users</div></div>
      <div class="stat-card"><div class="stat-num">${s.active_24h}</div><div class="stat-label">Active 24h</div></div>
      <div class="stat-card"><div class="stat-num">${s.messages}</div><div class="stat-label">Messages</div></div>
      <div class="stat-card"><div class="stat-num">${s.conversations}</div><div class="stat-label">Chats</div></div>
      <div class="stat-card"><div class="stat-num">${s.bans}</div><div class="stat-label">Bans</div></div>
    </div>
    <p style="text-align:center; color:var(--text-dim); font-size:12px;">Server: ${window.location.hostname}</p>
  `;
}

async function loadAdminUsers() {
  const r = await fetch('/api/admin/users');
  const j = await r.json();
  const rows = j.users.map(u => {
    let badges = '';
    if (u.is_owner) badges += '<span class="badge owner">OWNER</span> ';
    if (u.is_admin && !u.is_owner) badges += '<span class="badge admin">ADMIN</span> ';
    if (u.suspended) badges += '<span class="badge suspended">SUSPENDED</span> ';
    return `
      <tr>
        <td><b style="color:${u.nickname_color || 'var(--accent)'}">${escapeHtml(u.username)}</b><br>${badges}</td>
        <td><code style="font-size:11px;">${u.last_ip || '-'}</code></td>
        <td style="font-size:11px; color:var(--text-dim);">${u.last_seen ? new Date(u.last_seen).toLocaleDateString() : '-'}</td>
        <td>
          <button class="mini-btn" onclick="toggleUser('${u.id}','suspended',${!u.suspended})">${u.suspended ? 'Unsuspend' : 'Suspend'}</button>
          <button class="mini-btn" onclick="toggleUser('${u.id}','is_admin',${!u.is_admin})">${u.is_admin ? '−Admin' : '+Admin'}</button>
          <button class="mini-btn warn" onclick="showPunish('${u.id}','${escapeHtml(u.username)}')">Punish</button>
          <button class="mini-btn" onclick="adminResetPass('${u.id}')">Reset PW</button>
          <button class="mini-btn danger" onclick="banIP('${u.last_ip}')">Ban IP</button>
        </td>
      </tr>
    `;
  }).join('');
  document.getElementById('admin-users').innerHTML = `<table><thead><tr><th>User</th><th>IP</th><th>Last Seen</th><th>Actions</th></tr></thead><tbody>${rows}</tbody></table>`;
}

async function toggleUser(id, field, val) {
  await fetch(`/api/admin/user/${id}`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({[field]: val})
  });
  loadAdminUsers();
}

async function adminResetPass(id) {
  if (!confirm('Reset this user\'s password?')) return;
  const r = await fetch(`/api/admin/user/${id}/reset_password`, {method: 'POST'});
  const j = await r.json();
  prompt('New password (give this to the user):', j.new_password);
}

function showPunish(uid, uname) {
  showModal(`
    <h3>🔨 Punish ${escapeHtml(uname)}</h3>
    <select id="pun-type">
      <option value="warn">⚠️ Warning</option>
      <option value="mute">🔇 Mute (can't send messages)</option>
      <option value="ban">🚫 Ban (can't login)</option>
    </select>
    <input type="text" id="pun-reason" placeholder="Reason">
    <input type="number" id="pun-hours" placeholder="Hours (blank = permanent)">
    <button onclick="doPunish('${uid}')">Apply</button>
    <h4>Recent Punishments</h4>
    <div id="pun-list">Loading...</div>
  `);
  fetch(`/api/admin/user/${uid}/punishments`).then(r=>r.json()).then(j => {
    const list = document.getElementById('pun-list');
    if (!j.punishments.length) { list.innerHTML = '<p style="color:var(--text-dim)">No punishments</p>'; return; }
    list.innerHTML = j.punishments.map(p => `
      <div style="padding:8px; border-bottom:1px solid var(--border); font-size:12px;">
        <b>${p.type.toUpperCase()}</b> ${p.active ? '(active)' : '(expired)'} — ${escapeHtml(p.reason || 'no reason')}
        ${p.active ? `<button class="mini-btn" onclick="removePunishment('${p.id}','${uid}','${escapeHtml(uname)}')" style="float:right;">Remove</button>` : ''}
      </div>
    `).join('');
  });
}

async function doPunish(uid) {
  await fetch(`/api/admin/user/${uid}/punish`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({
      type: document.getElementById('pun-type').value,
      reason: document.getElementById('pun-reason').value,
      hours: parseInt(document.getElementById('pun-hours').value) || null
    })
  });
  alert('Punishment applied');
  closeModal();
}

async function removePunishment(pid, uid, uname) {
  await fetch(`/api/admin/punishment/${pid}/remove`, {method: 'POST'});
  showPunish(uid, uname);
}

async function loadAdminBans() {
  const r = await fetch('/api/admin/bans');
  const j = await r.json();
  const rows = j.bans.map(b => `
    <tr><td><code>${b.ip_address}</code></td><td>${escapeHtml(b.reason || '-')}</td>
    <td><button class="mini-btn" onclick="unban('${b.ip_address}')">Unban</button></td></tr>
  `).join('');
  document.getElementById('admin-bans').innerHTML = `
    <div style="display:flex; gap:8px; margin-bottom:16px;">
      <input type="text" id="ban-ip" placeholder="IP address" style="margin:0">
      <input type="text" id="ban-reason" placeholder="Reason" style="margin:0">
      <button style="width:auto; margin:0" onclick="manualBan()">Ban</button>
    </div>
    <table><thead><tr><th>IP</th><th>Reason</th><th></th></tr></thead>
    <tbody>${rows || '<tr><td colspan="3" style="text-align:center; color:var(--text-dim); padding:20px;">No bans</td></tr>'}</tbody></table>
  `;
}

async function manualBan() {
  const ip = document.getElementById('ban-ip').value.trim();
  const reason = document.getElementById('ban-reason').value.trim();
  if (!ip) return;
  await fetch('/api/admin/ban', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ip, reason})
  });
  loadAdminBans();
}

async function banIP(ip) {
  if (!ip || ip === '-') { alert('No IP available'); return; }
  const reason = prompt('Reason?') || '';
  await fetch('/api/admin/ban', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ip, reason})
  });
  loadAdminBans();
}

async function unban(ip) {
  await fetch('/api/admin/unban', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ip})
  });
  loadAdminBans();
}

async function loadAdminAnn() {
  const r = await fetch('/api/announcements');
  const j = await r.json();
  const rows = j.announcements.map(a => `
    <tr><td><b>${escapeHtml(a.title)}</b><br>${escapeHtml(a.content)}</td>
    <td><span class="badge ${a.priority}">${a.priority}</span></td>
    <td><button class="mini-btn danger" onclick="deleteAnn('${a.id}')">Delete</button></td></tr>
  `).join('');
  document.getElementById('admin-ann').innerHTML = `
    <div>
      <input type="text" id="ann-title" placeholder="Title">
      <textarea id="ann-content" placeholder="Content" rows="2"></textarea>
      <select id="ann-priority">
        <option value="info">Info (cyan)</option>
        <option value="warn">Warning (yellow)</option>
        <option value="critical">Critical (red)</option>
      </select>
      <button onclick="createAnn()">Push Announcement</button>
    </div>
    <h4>Active</h4>
    <table><tbody>${rows || '<tr><td style="text-align:center; color:var(--text-dim); padding:20px;">No active announcements</td></tr>'}</tbody></table>
  `;
}

async function createAnn() {
  const body = {
    title: document.getElementById('ann-title').value,
    content: document.getElementById('ann-content').value,
    priority: document.getElementById('ann-priority').value
  };
  if (!body.title || !body.content) return alert('Fill both fields');
  await fetch('/api/announcements', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify(body)
  });
  dismissedAnnouncements = [];
  localStorage.setItem('dismissedAnns', '[]');
  loadAdminAnn();
  loadAnnouncements();
}

async function deleteAnn(id) {
  await fetch(`/api/announcements/${id}`, {method: 'DELETE'});
  loadAdminAnn();
  loadAnnouncements();
}

async function loadAdminSettings() {
  const r = await fetch('/api/admin/settings');
  const s = (await r.json()).settings;
  document.getElementById('admin-settings').innerHTML = `
    <div class="setting-row">
      <div><div class="setting-label">Signups enabled</div><div class="setting-desc">Allow new users to register</div></div>
      <input type="checkbox" id="s-signups" ${s.signups_enabled ? 'checked' : ''}>
    </div>
    <div class="setting-row">
      <div><div class="setting-label">Invites enabled</div><div class="setting-desc">Allow users to create invite links</div></div>
      <input type="checkbox" id="s-invites" ${s.invites_enabled ? 'checked' : ''}>
    </div>
    <div class="setting-row">
      <div><div class="setting-label">Invite mode</div><div class="setting-desc">Who can create invites</div></div>
      <select id="s-mode">
        <option value="everyone" ${s.invite_creation_mode==='everyone'?'selected':''}>Everyone</option>
        <option value="admins_only" ${s.invite_creation_mode==='admins_only'?'selected':''}>Admins only</option>
      </select>
    </div>
    <div class="setting-row">
      <div><div class="setting-label">Retention days</div><div class="setting-desc">How long messages last</div></div>
      <input type="number" id="s-days" value="${s.default_retention_days}">
    </div>
    <div class="setting-row">
      <div><div class="setting-label">Maintenance mode</div><div class="setting-desc">Block non-admin access</div></div>
      <input type="checkbox" id="s-maint" ${s.maintenance_mode ? 'checked' : ''}>
    </div>
    <button onclick="saveAdminSettings()" style="margin-top:16px;">💾 Save Settings</button>
  `;
}

async function saveAdminSettings() {
  const body = {
    signups_enabled: document.getElementById('s-signups').checked,
    invites_enabled: document.getElementById('s-invites').checked,
    invite_creation_mode: document.getElementById('s-mode').value,
    default_retention_days: parseInt(document.getElementById('s-days').value),
    maintenance_mode: document.getElementById('s-maint').checked
  };
  await fetch('/api/admin/settings', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify(body)
  });
  alert('Saved!');
}

async function loadAdminAudit() {
  const r = await fetch('/api/admin/audit');
  const j = await r.json();
  document.getElementById('admin-audit').innerHTML = j.logs.map(l => `
    <div class="audit-entry">
      <span class="who">${escapeHtml(l.admin_username || 'system')}</span> did
      <b>${l.action}</b> ${l.target_type ? 'on ' + l.target_type : ''}
      ${l.details ? '<br><span style="color:var(--text-dim); font-size:11px;">' + escapeHtml(l.details) + '</span>' : ''}
      <div class="when">${new Date(l.created_at).toLocaleString()} · ${l.ip_address || '-'}</div>
    </div>
  `).join('') || '<p style="text-align:center; color:var(--text-dim); padding:20px;">No logs yet</p>';
}

// ============ UTILS ============
function showModal(html) {
  document.getElementById('modal-content').innerHTML = html;
  document.getElementById('modal-bg').classList.remove('hidden');
}
function closeModal() {
  document.getElementById('modal-bg').classList.add('hidden');
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
}

// Start
checkSession();
