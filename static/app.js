let currentUser = null;
let currentConv = null;
let pollTimer = null;

// ============ AUTH ============
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
  enterApp();
};

document.getElementById('logout-btn').onclick = async () => {
  await fetch('/api/logout', {method: 'POST'});
  location.reload();
};

// ============ APP ============
async function checkSession() {
  const r = await fetch('/api/me');
  const j = await r.json();
  if (j.user) {
    currentUser = j.user;
    enterApp();
  }
}

function enterApp() {
  document.getElementById('auth-screen').classList.add('hidden');
  document.getElementById('app-screen').classList.remove('hidden');
  document.getElementById('my-username').textContent = currentUser.username;
  if (currentUser.is_admin || currentUser.is_owner) {
    document.getElementById('admin-btn').classList.remove('hidden');
  }
  loadConversations();
}

async function loadConversations() {
  const r = await fetch('/api/conversations');
  const j = await r.json();
  const list = document.getElementById('conv-list');
  list.innerHTML = '';
  j.conversations.forEach(c => {
    const others = c.members.filter(m => m.id !== currentUser.id).map(m => m.username).join(', ');
    const div = document.createElement('div');
    div.className = 'conv-item';
    div.innerHTML = `<div class="conv-name">${others || 'Empty chat'}</div>`;
    div.onclick = () => openConversation(c.id, others);
    list.appendChild(div);
  });
}

async function openConversation(id, title) {
  currentConv = id;
  document.querySelectorAll('.conv-item').forEach(x => x.classList.remove('active'));
  event.currentTarget.classList.add('active');
  document.getElementById('empty-state').classList.add('hidden');
  document.getElementById('chat-view').classList.remove('hidden');
  document.getElementById('chat-title').textContent = title || 'Chat';
  await loadMessages();
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(loadMessages, 3000);
}

async function loadMessages() {
  if (!currentConv) return;
  const r = await fetch(`/api/messages/${currentConv}`);
  const j = await r.json();
  const box = document.getElementById('messages');
  const wasAtBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 50;
  box.innerHTML = '';
  j.messages.forEach(m => {
    const div = document.createElement('div');
    div.className = 'msg ' + (m.sender_id === currentUser.id ? 'mine' : 'theirs');
    const sender = m.users?.username || 'unknown';
    const time = new Date(m.created_at).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
    div.innerHTML = `<div>${escapeHtml(m.content || '')}</div><div class="msg-meta">${sender} · ${time}</div>`;
    box.appendChild(div);
  });
  if (wasAtBottom) box.scrollTop = box.scrollHeight;
}

document.getElementById('msg-form').onsubmit = async (e) => {
  e.preventDefault();
  const input = document.getElementById('msg-input');
  const content = input.value.trim();
  if (!content || !currentConv) return;
  input.value = '';
  await fetch(`/api/messages/${currentConv}`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({content})
  });
  loadMessages();
};

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

// ============ INVITES ============
document.getElementById('invites-btn').onclick = async () => {
  const r = await fetch('/api/invites');
  const j = await r.json();
  const rows = j.invites.map(i => `
    <tr>
      <td><code>${i.code}</code></td>
      <td>${i.uses_count}${i.max_uses ? '/'+i.max_uses : ''}</td>
      <td>${i.revoked ? '❌ Revoked' : '✅ Active'}</td>
      <td>
        <button class="mini-btn" onclick="copyInvite('${i.code}')">Copy</button>
        ${!i.revoked ? `<button class="mini-btn danger" onclick="revokeInvite('${i.id}')">Revoke</button>` : ''}
      </td>
    </tr>
  `).join('');
  showModal(`
    <h3>Your Invite Links</h3>
    <div style="display:flex; gap:8px; margin-bottom:16px;">
      <input type="number" id="inv-uses" placeholder="Max uses (blank = unlimited)" style="margin:0">
      <input type="number" id="inv-hours" placeholder="Expires in hours" style="margin:0">
      <button style="width:auto; margin:0;" onclick="makeInvite()">Create</button>
    </div>
    <table>
      <thead><tr><th>Code</th><th>Uses</th><th>Status</th><th>Actions</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="4" style="text-align:center; color:var(--text-dim)">No invites yet</td></tr>'}</tbody>
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

// ============ ADMIN ============
document.getElementById('admin-btn').onclick = async () => {
  const [uRes, bRes, sRes] = await Promise.all([
    fetch('/api/admin/users').then(r => r.json()),
    fetch('/api/admin/bans').then(r => r.json()),
    fetch('/api/admin/settings').then(r => r.json())
  ]);
  const s = sRes.settings;
  const userRows = uRes.users.map(u => `
    <tr>
      <td>${u.username}${u.is_owner ? ' 👑' : (u.is_admin ? ' ⚙️' : '')}</td>
      <td><code>${u.last_ip || '-'}</code></td>
      <td>
        <button class="mini-btn ${u.suspended ? 'danger' : ''}" onclick="toggleUser('${u.id}','suspended',${!u.suspended})">${u.suspended ? 'Unsuspend' : 'Suspend'}</button>
        <button class="mini-btn" onclick="toggleUser('${u.id}','is_admin',${!u.is_admin})">${u.is_admin ? '-Admin' : '+Admin'}</button>
        <button class="mini-btn" onclick="toggleUser('${u.id}','keep_all_forever',${!u.keep_all_forever})">${u.keep_all_forever ? 'Un-Keep' : 'Keep All'}</button>
        <button class="mini-btn danger" onclick="banIP('${u.last_ip}')">Ban IP</button>
      </td>
    </tr>
  `).join('');
  const banRows = bRes.bans.map(b => `
    <tr><td><code>${b.ip_address}</code></td><td>${b.reason || '-'}</td>
    <td><button class="mini-btn" onclick="unban('${b.ip_address}')">Unban</button></td></tr>
  `).join('');

  showModal(`
    <h3>⚙️ Admin Panel</h3>
    <h4 style="margin-top:16px;">Settings</h4>
    <label>Retention days: <input type="number" id="s-days" value="${s.default_retention_days}" style="width:80px; margin:4px;"></label>
    <label><input type="checkbox" id="s-signups" ${s.signups_enabled ? 'checked' : ''}> Signups enabled</label>
    <label><input type="checkbox" id="s-invites" ${s.invites_enabled ? 'checked' : ''}> Invites enabled</label>
    <label>Invite mode:
      <select id="s-mode">
        <option value="everyone" ${s.invite_creation_mode==='everyone'?'selected':''}>Everyone</option>
        <option value="admins_only" ${s.invite_creation_mode==='admins_only'?'selected':''}>Admins only</option>
      </select>
    </label>
    <button style="width:auto; margin-top:8px;" onclick="saveSettings()">Save Settings</button>

    <h4 style="margin-top:20px;">Users</h4>
    <table><thead><tr><th>User</th><th>IP</th><th>Actions</th></tr></thead><tbody>${userRows}</tbody></table>

    <h4 style="margin-top:20px;">Banned IPs</h4>
    <table><thead><tr><th>IP</th><th>Reason</th><th></th></tr></thead><tbody>${banRows || '<tr><td colspan="3" style="text-align:center; color:var(--text-dim)">No bans</td></tr>'}</tbody></table>
  `);
};

async function toggleUser(id, field, val) {
  await fetch(`/api/admin/user/${id}`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({[field]: val})
  });
  document.getElementById('admin-btn').click();
}

async function banIP(ip) {
  if (!ip || ip === '-') { alert('No IP available'); return; }
  const reason = prompt('Reason?') || '';
  await fetch('/api/admin/ban', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ip, reason})
  });
  document.getElementById('admin-btn').click();
}

async function unban(ip) {
  await fetch('/api/admin/unban', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ip})
  });
  document.getElementById('admin-btn').click();
}

async function saveSettings() {
  const body = {
    default_retention_days: parseInt(document.getElementById('s-days').value),
    signups_enabled: document.getElementById('s-signups').checked,
    invites_enabled: document.getElementById('s-invites').checked,
    invite_creation_mode: document.getElementById('s-mode').value
  };
  await fetch('/api/admin/settings', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify(body)
  });
  alert('Saved!');
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
// ============ CREDITS ============
function showCredits() {
  showModal(`
    <div class="credits-box">
      <div class="credits-logo">CIPHER</div>
      <div class="credits-version">v0.1.0 · BETA · БЕТА</div>

      <div class="credits-line">— A solo project by —</div>
      <div class="credits-name">STEPUNDRIK</div>
      <div class="credits-roles">
        Frontend · Backend · Database<br>
        Design · Deployment · Everything
      </div>

      <div class="credits-line">Built from scratch. Zero team. Full vision.</div>

      <div class="credits-heart">Made with 🖤 and too much cyan.</div>
      <div class="credits-copy">© 2025 Stepundrik · All rights reserved</div>
    </div>
  `);
}
