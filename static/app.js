/* ============================================
   CIPHER — Core Application Logic (v1.0.0)
   ============================================ */

// --- State Management ---
let currentUser = null;
let currentConvId = null;
let pollTimer = null;
let typingTimer = null;
let pendingImage = null;
let lastMessageTime = null;
let dismissedAnnouncements = JSON.parse(localStorage.getItem('dismissedAnns') || '[]');

// --- 1. Initialization ---
document.addEventListener('DOMContentLoaded', () => {
  checkSession();
  initAuthTabs();
});

// --- 2. Auth Logic ---
function initAuthTabs() {
  const tabs = document.querySelectorAll('.auth-card .tab');
  tabs.forEach(tab => {
    tab.onclick = () => {
      tabs.forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      const isSignup = tab.dataset.tab === 'signup';
      document.getElementById('invite-field').classList.toggle('hidden', !isSignup);
      document.getElementById('auth-submit').textContent = isSignup ? 'Create account' : 'Sign in';
    };
  });

  // Auto-fill invite from URL
  const invite = new URLSearchParams(window.location.search).get('invite');
  if (invite) {
    document.querySelector('[data-tab="signup"]').click();
    document.getElementById('auth-invite').value = invite;
  }
}

document.getElementById('auth-form').onsubmit = async (e) => {
  e.preventDefault();
  const errorEl = document.getElementById('auth-error');
  errorEl.textContent = '';
  
  const isSignup = document.querySelector('.auth-card .tab.active').dataset.tab === 'signup';
  const data = {
    username: document.getElementById('auth-username').value,
    password: document.getElementById('auth-password').value
  };
  if (isSignup) data.invite_code = document.getElementById('auth-invite').value;

  try {
    const r = await fetch(isSignup ? '/api/signup' : '/api/login', {
      method: 'POST', 
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data)
    });
    const j = await r.json();

    if (!r.ok) {
      errorEl.textContent = j.error || 'Connection failed';
      return;
    }

    currentUser = j.user;
    if (isSignup && j.recovery_phrase) {
      showRecoverySetup(j.recovery_phrase, j.recovery_key);
    } else {
      enterApp();
    }
  } catch (err) {
    errorEl.textContent = 'Server unreachable';
  }
};

// --- 3. App Core ---
async function checkSession() {
  const r = await fetch('/api/me');
  const j = await r.json();
  if (j.user) {
    currentUser = j.user;
    enterApp();
  }
  loadAnnouncements();
}

function enterApp() {
  document.getElementById('auth-screen').classList.add('hidden');
  document.getElementById('app-screen').classList.remove('hidden');
  document.getElementById('my-username').textContent = currentUser.username;
  document.getElementById('empty-username').textContent = currentUser.username;
  
  // Set user avatar
  const avatar = document.getElementById('me-avatar');
  avatar.textContent = currentUser.username[0];
  avatar.style.background = `linear-gradient(135deg, ${currentUser.nickname_color}, #6366f1)`;

  if (currentUser.theme_color) updateTheme(currentUser.theme_color);
  if (currentUser.is_admin || currentUser.is_owner) {
    document.getElementById('admin-btn').classList.remove('hidden');
  }

  loadConversations();
}

function updateTheme(color) {
  document.documentElement.style.setProperty('--accent', color);
  // Calculate glow
  const r = parseInt(color.slice(1,3), 16), g = parseInt(color.slice(3,5), 16), b = parseInt(color.slice(5,7), 16);
  document.documentElement.style.setProperty('--accent-glow', `rgba(${r},${g},${b},0.35)`);
  document.documentElement.style.setProperty('--accent-soft', `rgba(${r},${g},${b},0.1)`);
}

// --- 4. Messaging ---
async function loadConversations() {
  const r = await fetch('/api/conversations');
  const j = await r.json();
  const list = document.getElementById('conv-list');
  list.innerHTML = '';
  
  j.conversations.forEach(c => {
    const other = c.members.find(m => m.id !== currentUser.id) || {username: 'Unknown'};
    const div = document.createElement('div');
    div.className = `conv-item ${c.id === currentConvId ? 'active' : ''}`;
    div.innerHTML = `
      <div class="conv-avatar" style="background: linear-gradient(135deg, ${other.nickname_color}, var(--bg-5))">
        ${other.username[0]}
      </div>
      <div class="conv-body">
        <div class="conv-top">
          <span class="conv-name">${escapeHtml(other.username)}</span>
          <span class="conv-time">${c.last_time ? formatShortTime(c.last_time) : ''}</span>
        </div>
        <div class="conv-preview">${escapeHtml(c.last_message || 'No messages yet')}</div>
      </div>
      ${c.muted ? '<div class="conv-muted-icon">🔕</div>' : ''}
    `;
    div.onclick = () => openChat(c.id, other.username, other.nickname_color);
    list.appendChild(div);
  });
}

function openChat(id, name, color) {
  currentConvId = id;
  document.getElementById('empty-state').classList.add('hidden');
  document.getElementById('chat-view').classList.remove('hidden');
  document.getElementById('chat-title').textContent = name;
  
  const cavatar = document.getElementById('chat-avatar');
  cavatar.textContent = name[0];
  cavatar.style.background = `linear-gradient(135deg, ${color}, var(--bg-5))`;
  
  loadMessages();
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => { loadMessages(); checkTyping(); }, 3000);
  
  loadConversations(); // Update active state in list
}

async function loadMessages() {
  if (!currentConvId) return;
  const r = await fetch(`/api/messages/${currentConvId}`);
  const j = await r.json();
  const box = document.getElementById('messages');
  const wasAtBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 100;
  
  // Only re-render if data changed
  const newHash = JSON.stringify(j.messages);
  if (box.dataset.hash === newHash) return;
  box.dataset.hash = newHash;

  box.innerHTML = '';
  let lastSender = null;
  let lastTime = null;

  j.messages.forEach(m => {
    const isMine = m.sender_id === currentUser.id;
    const time = new Date(m.created_at);
    
    // Date Divider
    if (!lastTime || time.toDateString() !== lastTime.toDateString()) {
      box.innerHTML += `<div class="date-divider">${formatDate(time)}</div>`;
    }

    // Message Grouping
    const showHeader = lastSender !== m.sender_id || (time - lastTime) > 300000;
    const groupDiv = document.createElement('div');
    groupDiv.className = `msg-group ${isMine ? 'mine' : 'theirs'}`;
    
    let html = '';
    if (showHeader && !isMine) {
      const senderName = m.is_anonymous ? 'Anonymous' : (m.users?.username || 'Unknown');
      html += `<div class="msg-sender" style="color: ${m.users?.nickname_color || 'var(--accent)'}">${escapeHtml(senderName)}</div>`;
    }
    
    html += `<div class="msg">`;
    if (m.image_url) html += `<img src="${m.image_url}" class="msg-img" onclick="viewImage('${m.image_url}')">`;
    if (m.content) html += `<div class="msg-text">${escapeHtml(m.content)}</div>`;
    html += `</div>`;

    // Reactions & Meta
    html += `<div class="msg-meta">
      <span class="msg-time">${formatTime(time)}</span>
      <button class="react-btn" onclick="showEmojiPicker(event, '${m.id}')">＋</button>
      ${isMine && m.read_by?.length > 0 ? '<span class="msg-seen" title="Seen by everyone">✓✓</span>' : ''}
    </div>`;

    if (m.reactions?.length > 0) {
      html += `<div class="msg-reactions">${renderReactions(m.id, m.reactions)}</div>`;
    }

    groupDiv.innerHTML = html;
    box.appendChild(groupDiv);

    lastSender = m.sender_id;
    lastTime = time;
  });

  if (wasAtBottom) box.scrollTop = box.scrollHeight;
}

function renderReactions(msgId, reactions) {
  const counts = {};
  reactions.forEach(r => {
    counts[r.emoji] = counts[r.emoji] || {count: 0, mine: false};
    counts[r.emoji].count++;
    if (r.user_id === currentUser.id) counts[r.emoji].mine = true;
  });
  return Object.entries(counts).map(([emoji, data]) => `
    <span class="reaction ${data.mine ? 'mine' : ''}" onclick="toggleReact('${msgId}', '${emoji}')">
      ${emoji} <span>${data.count}</span>
    </span>
  `).join('');
}

// --- 5. Composer & Interaction ---
document.getElementById('msg-form').onsubmit = async (e) => {
  e.preventDefault();
  const input = document.getElementById('msg-input');
  const content = input.value.trim();
  if (!content && !pendingImage) return;

  const data = { content };
  if (pendingImage) data.image_data = pendingImage;
  if (currentUser.anonymous_mode) data.anonymous = true;

  input.value = '';
  clearImage();

  try {
    const r = await fetch(`/api/messages/${currentConvId}`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data)
    });
    if (r.ok) loadMessages();
    else {
      const j = await r.json();
      showToast(j.error || 'Failed to send', 'error');
    }
  } catch (err) {
    showToast('Network error', 'error');
  }
};

document.getElementById('msg-input').oninput = () => {
  if (!currentConvId || typingTimer) return;
  fetch(`/api/typing/${currentConvId}`, {method: 'POST'});
  typingTimer = setTimeout(() => { typingTimer = null; }, 4000);
};

async function checkTyping() {
  if (!currentConvId) return;
  const r = await fetch(`/api/typing/${currentConvId}`);
  const j = await r.json();
  const el = document.getElementById('typing-indicator');
  const text = document.getElementById('typing-text');
  
  if (j.typing.length > 0) {
    el.classList.remove('hidden');
    text.textContent = `${j.typing.join(', ')} is typing...`;
  } else {
    el.classList.add('hidden');
  }
}

// --- 6. Tool Features ---

// New Chat
document.getElementById('new-chat-btn').onclick = () => {
  showModal(`
    <h3>Start a <span class="accent">New Chat</span></h3>
    <p>Enter the username of the person you want to message.</p>
    <div class="field" style="margin-top: 20px">
      <input type="text" id="new-chat-user" placeholder="Enter username..." autofocus>
    </div>
    <button class="btn-primary" onclick="createNewChat()">Start Conversation</button>
    <p id="new-chat-error" class="form-msg error"></p>
  `);
};

async function createNewChat() {
  const username = document.getElementById('new-chat-user').value.trim();
  if (!username) return;
  const r = await fetch('/api/conversations/new', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({username})
  });
  const j = await r.json();
  if (!r.ok) {
    document.getElementById('new-chat-error').textContent = j.error;
    return;
  }
  closeModal();
  openChat(j.conversation_id, username, '#00d9ff');
}

// Settings
document.getElementById('settings-btn').onclick = async () => {
  showModal(`
    <h3>Personal <span class="accent">Settings</span></h3>
    
    <h4>Identity & Style</h4>
    <div class="color-row">
      <label>Nickname Color</label>
      <input type="color" id="s-nick-color" value="${currentUser.nickname_color}">
    </div>
    <div class="color-row">
      <label>Theme Accent</label>
      <input type="color" id="s-theme-color" value="${currentUser.theme_color}">
    </div>

    <h4>Privacy & Persistence</h4>
    <div class="setting-row">
      <div class="setting-info">
        <div class="setting-label">Anonymous Mode</div>
        <div class="setting-desc">Hide your name on new messages</div>
      </div>
      <input type="checkbox" id="s-anon" ${currentUser.anonymous_mode ? 'checked' : ''}>
    </div>
    <div class="setting-row">
      <div class="setting-info">
        <div class="setting-label">Preserve History</div>
        <div class="setting-desc">Don't auto-delete my messages</div>
      </div>
      <input type="checkbox" id="s-keep" ${currentUser.keep_all_forever ? 'checked' : ''}>
    </div>

    <h4>Account Security</h4>
    <button class="btn-secondary" onclick="showChangePassModal()" style="margin-bottom: 8px">Change Password</button>
    <button class="btn-secondary btn-danger" onclick="showPanicModal()">💥 Panic Button</button>

    <div style="margin-top: 24px">
      <button class="btn-primary" onclick="saveUserSettings()">Save All Changes</button>
    </div>
  `);
};

async function saveUserSettings() {
  const body = {
    nickname_color: document.getElementById('s-nick-color').value,
    theme_color: document.getElementById('s-theme-color').value,
    anonymous_mode: document.getElementById('s-anon').checked,
    keep_all_forever: document.getElementById('s-keep').checked
  };
  const r = await fetch('/api/profile', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  if (r.ok) {
    Object.assign(currentUser, body);
    updateTheme(body.theme_color);
    showToast('Settings updated', 'success');
    closeModal();
    loadConversations();
  }
}

// --- 7. Admin Dashboard ---
document.getElementById('admin-btn').onclick = () => {
  showModal(`
    <h3 style="margin-bottom: 20px">👑 Owner <span class="accent">Dashboard</span></h3>
    <div class="admin-tabs">
      <button class="admin-tab active" onclick="switchAdminTab(this, 'stats')">📊 Stats</button>
      <button class="admin-tab" onclick="switchAdminTab(this, 'users')">👥 Users</button>
      <button class="admin-tab" onclick="switchAdminTab(this, 'bans')">🔨 Bans</button>
      <button class="admin-tab" onclick="switchAdminTab(this, 'news')">📢 Alerts</button>
      <button class="admin-tab" onclick="switchAdminTab(this, 'conf')">⚙️ Config</button>
    </div>
    <div id="admin-content" class="admin-content"></div>
  `, true);
  switchAdminTab(null, 'stats');
};

async function switchAdminTab(btn, tab) {
  if (btn) {
    document.querySelectorAll('.admin-tab').forEach(t => t.classList.remove('active'));
    btn.classList.add('active');
  }
  const container = document.getElementById('admin-content');
  container.innerHTML = '<div style="padding: 40px; text-align: center; opacity: 0.5;">Loading...</div>';

  if (tab === 'stats') {
    const r = await fetch('/api/admin/stats');
    const s = await r.json();
    container.innerHTML = `
      <div class="stats-grid">
        <div class="stat-card"><div class="stat-num">${s.users}</div><div class="stat-label">Users</div></div>
        <div class="stat-card"><div class="stat-num">${s.active_24h}</div><div class="stat-label">Active 24h</div></div>
        <div class="stat-card"><div class="stat-num">${s.messages}</div><div class="stat-label">Messages</div></div>
        <div class="stat-card"><div class="stat-num">${s.conversations}</div><div class="stat-label">Chats</div></div>
      </div>
    `;
  } else if (tab === 'users') {
    const r = await fetch('/api/admin/users');
    const j = await r.json();
    container.innerHTML = `
      <table>
        <thead><tr><th>Username</th><th>Last IP</th><th>Actions</th></tr></thead>
        <tbody>${j.users.map(u => `
          <tr>
            <td>
              <span style="font-weight:600; color:${u.nickname_color}">${u.username}</span>
              ${u.is_owner ? '<span class="badge owner">Owner</span>' : (u.is_admin ? '<span class="badge admin">Admin</span>' : '')}
            </td>
            <td><code>${u.last_ip || '-'}</code></td>
            <td>
              <button class="mini-btn ${u.suspended ? 'warn' : ''}" onclick="adminToggleUser('${u.id}', 'suspended', ${!u.suspended})">${u.suspended ? 'Unsuspend' : 'Suspend'}</button>
              <button class="mini-btn danger" onclick="adminBanIP('${u.last_ip}')">Ban IP</button>
            </td>
          </tr>
        `).join('')}</tbody>
      </table>
    `;
  } else if (tab === 'news') {
    const r = await fetch('/api/announcements');
    const j = await r.json();
    container.innerHTML = `
      <div class="field"><input type="text" id="ann-title" placeholder="Title"></div>
      <div class="field"><textarea id="ann-body" placeholder="Message content"></textarea></div>
      <div class="field"><select id="ann-pri"><option value="info">Info</option><option value="warn">Warning</option><option value="critical">Critical</option></select></div>
      <button class="btn-primary" onclick="adminPostAnn()">Post Announcement</button>
      <h4>Active Alerts</h4>
      ${j.announcements.map(a => `
        <div class="audit-entry">
          <span class="badge ${a.priority}">${a.priority}</span> <b>${a.title}</b>
          <button class="mini-btn danger" style="float:right" onclick="adminDeleteAnn('${a.id}')">Delete</button>
        </div>
      `).join('') || '<p>No active alerts</p>'}
    `;
  }
}

// --- 8. Utilities ---
function showModal(content, wide = false) {
  const bg = document.getElementById('modal-bg');
  const modal = bg.querySelector('.modal');
  modal.className = `modal ${wide ? 'wide' : ''}`;
  document.getElementById('modal-content').innerHTML = content;
  bg.classList.remove('hidden');
}

function closeModal() {
  document.getElementById('modal-bg').classList.add('hidden');
}

function showToast(msg, type = 'info') {
  const container = document.getElementById('toast-container');
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.textContent = msg;
  container.appendChild(toast);
  setTimeout(() => {
    toast.classList.add('fade-out');
    setTimeout(() => toast.remove(), 300);
  }, 4000);
}

async function handleImageSelect(e) {
  const file = e.target.files[0];
  if (!file) return;
  if (!file.type.startsWith('image/')) {
    showToast('Only image files are allowed', 'error');
    return;
  }
  
  showToast('Optimizing image...', 'info');
  const compressed = await compressImage(file);
  pendingImage = compressed;
  
  const preview = document.getElementById('image-preview');
  preview.innerHTML = `
    <img src="${compressed}">
    <button class="btn-secondary" onclick="clearImage()">Remove</button>
  `;
  preview.classList.remove('hidden');
}

function clearImage() {
  pendingImage = null;
  document.getElementById('image-preview').classList.add('hidden');
  document.getElementById('img-input').value = '';
}

function compressImage(file) {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.readAsDataURL(file);
    reader.onload = (event) => {
      const img = new Image();
      img.src = event.target.result;
      img.onload = () => {
        const canvas = document.createElement('canvas');
        const MAX_WIDTH = 1200;
        const scaleSize = MAX_WIDTH / img.width;
        canvas.width = MAX_WIDTH;
        canvas.height = img.height * scaleSize;
        const ctx = canvas.getContext('2d');
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        resolve(canvas.toDataURL('image/jpeg', 0.7));
      };
    };
  });
}

function viewImage(url) {
  const v = document.getElementById('img-viewer');
  document.getElementById('img-viewer-img').src = url;
  v.classList.remove('hidden');
}

// Formatting
function formatShortTime(iso) {
  const d = new Date(iso);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) {
    return d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
  }
  return d.toLocaleDateString([], {month: 'short', day: 'numeric'});
}

function formatTime(date) {
  return date.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
}

function formatDate(date) {
  return date.toLocaleDateString([], {weekday: 'long', month: 'long', day: 'numeric'});
}

function escapeHtml(unsafe) {
  return unsafe
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#039;");
}

async function toggleReact(msgId, emoji) {
  await fetch(`/api/messages/${msgId}/react`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({emoji})
  });
  loadMessages();
}

function showEmojiPicker(e, msgId) {
  e.stopPropagation();
  const emojis = ['👍','❤️','😂','😮','😢','🔥','💯','👀'];
  const picker = document.createElement('div');
  picker.className = 'emoji-picker';
  emojis.forEach(em => {
    const b = document.createElement('button');
    b.textContent = em;
    b.onclick = () => { toggleReact(msgId, em); picker.remove(); };
    picker.appendChild(b);
  });
  document.body.appendChild(picker);
  const rect = e.target.getBoundingClientRect();
  picker.style.top = (rect.top - 50) + 'px';
  picker.style.left = rect.left + 'px';
  setTimeout(() => {
    document.addEventListener('click', () => picker.remove(), {once: true});
  }, 10);
}

async function loadAnnouncements() {
  const r = await fetch('/api/announcements');
  const j = await r.json();
  const bar = document.getElementById('announcement-bar');
  const next = j.announcements.find(a => !dismissedAnnouncements.includes(a.id));
  if (!next) { bar.classList.add('hidden'); return; }
  bar.className = `announcement-bar ${next.priority}`;
  bar.innerHTML = `
    <span><b>${escapeHtml(next.title)}</b>: ${escapeHtml(next.content)}</span>
    <button class="close-ann" onclick="dismissAnnouncement('${next.id}')">×</button>
  `;
  bar.classList.remove('hidden');
}

function dismissAnnouncement(id) {
  dismissedAnnouncements.push(id);
  localStorage.setItem('dismissedAnns', JSON.stringify(dismissedAnnouncements));
  document.getElementById('announcement-bar').classList.add('hidden');
}

window.showCredits = function() {
  showModal(`
    <div class="credits-box">
      <div class="credits-logo">CIPHER</div>
      <div class="credits-version">Production Build v1.0.0</div>
      <div class="credits-line">— Solo Developer —</div>
      <div class="credits-name">STEPUNDRIK</div>
      <div class="credits-roles">Architecture · Interface · Security<br>Data Science · Protocol Design</div>
      <div class="credits-heart">Private Messaging Reimagined.</div>
      <div class="credits-copy">© 2025 · All rights reserved.</div>
    </div>
  `);
}

// Recover/setup logic
function showRecoverySetup(phrase, key) {
  showModal(`
    <h3>🔐 Security <span class="accent">Protocol</span></h3>
    <p>Your account is created. For your protection, you must save these recovery credentials. We do not store your password.</p>
    <div class="recovery-box">
      <div class="recovery-title">Recovery Phrase</div>
      <div class="recovery-content">${phrase}</div>
      <button class="mini-btn" onclick="copyText('${phrase}')">Copy Words</button>
    </div>
    <div class="recovery-box">
      <div class="recovery-title">Key Backup File</div>
      <p class="recovery-desc">Highly recommended for device loss.</p>
      <button class="btn-primary download-btn" onclick="downloadKey('${currentUser.username}', '${key}')">Download Backup</button>
    </div>
    <button class="btn-primary" id="rec-done" disabled style="opacity:0.3; margin-top:10px">I have saved these</button>
  `);
  
  let saved = false;
  window.downloadKey = (u, k) => {
    const blob = new Blob([k], {type: 'text/plain'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `cipher_${u}.key`;
    a.click();
    saved = true;
    document.getElementById('rec-done').disabled = false;
    document.getElementById('rec-done').style.opacity = 1;
  };
  
  document.getElementById('rec-done').onclick = () => {
    closeModal();
    enterApp();
  }
}
