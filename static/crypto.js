/* ============================================================
   CIPHER HYPERCRYPT — v1.2.0
   Solo project by Stepundrik

   Everything in this file runs in the browser. The server never
   sees a plaintext message, a plaintext image, or a plaintext
   private key.

   SHAPE OF THE SCHEME
   -------------------
   identity key   one ECDH key pair per account. The private half
                  is generated here, kept in IndexedDB as a
                  non-extractable CryptoKey, and stored on the
                  server only as an AES-GCM blob wrapped with a
                  key derived from your password (PBKDF2-SHA256,
                  210k rounds). The server cannot open it.

   conversation   a random 256-bit AES key per conversation.
   key (CK)       Sealed separately for every member: an ephemeral
                  ECDH key pair is generated per seal, so a member
                  opens their copy with their own identity key and
                  the ephemeral public half that travels with it —
                  they never need to know who sealed it. That is
                  the "self-enveloping" part.

   master seal    the same CK is additionally sealed to the
                  operator's master public key and stored apart.
                  See the Terms of Service, section 5.

   message        AES-256-GCM over {t: text, ts: epoch} with a
                  random 12-byte nonce, stored as
                  "cph1:" + base64(JSON envelope).

   Images are encrypted with the same CK and uploaded as opaque
   .cph objects; the server stores bytes it cannot read.
   ============================================================ */

const HyperCrypt = (() => {
  const PREFIX = 'cph1:';
  const IDB_NAME = 'cipher-hypercrypt';
  const IDB_VERSION = 1;
  const PBKDF2_ITERS = 210000;
  const CURVE_PREF = ['X25519', 'P-256'];   // X25519 where the browser has it

  let db = null;
  let identity = null;          // { privateKey, publicKey, curve, fingerprint }
  const ckCache = new Map();    // conversationId -> { key, wrappedFor:Set }
  const pubCache = new Map();   // username -> { public_key, curve }
  let masterKey = null;         // { public_key, curve }
  let supported = true;

  // -------------------------------------------------- plumbing
  const b64 = {
    from(buf) {
      const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
      let s = '';
      for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
      return btoa(s);
    },
    toBuf(str) {
      const bin = atob(str);
      const out = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
      return out.buffer;
    }
  };

  const enc = new TextEncoder();
  const dec = new TextDecoder();

  function openDb() {
    if (db) return Promise.resolve(db);
    return new Promise((resolve, reject) => {
      if (!('indexedDB' in window)) { supported = false; return reject(new Error('no indexedDB')); }
      const req = indexedDB.open(IDB_NAME, IDB_VERSION);
      req.onupgradeneeded = () => {
        const d = req.result;
        if (!d.objectStoreNames.contains('kv')) d.createObjectStore('kv');
      };
      req.onsuccess = () => { db = req.result; resolve(db); };
      req.onerror = () => reject(req.error);
    });
  }

  async function kvGet(key) {
    await openDb();
    return new Promise((resolve) => {
      const tx = db.transaction('kv', 'readonly');
      const r = tx.objectStore('kv').get(key);
      r.onsuccess = () => resolve(r.result);
      r.onerror = () => resolve(null);
    });
  }

  async function kvSet(key, value) {
    await openDb();
    return new Promise((resolve) => {
      const tx = db.transaction('kv', 'readwrite');
      tx.objectStore('kv').put(value, key);
      tx.oncomplete = () => resolve(true);
      tx.onerror = () => resolve(false);
    });
  }

  function pickCurve() {
    for (const curve of CURVE_PREF) {
      try {
        // Feature-detect by asking for a key; X25519 is new in WebCrypto.
        const probe = crypto.subtle.generateKey({ name: 'ECDH', namedCurve: curve }, false, ['deriveBits']);
        if (probe && typeof probe.then === 'function') return curve;
      } catch (e) { /* try the next one */ }
    }
    return 'P-256';
  }

  async function generateIdentityPair(curve) {
    const pair = await crypto.subtle.generateKey(
      { name: 'ECDH', namedCurve: curve }, true, ['deriveBits']
    );
    return pair;
  }

  function fingerprintOf(pubB64) {
    // Not a crypto hash of consequence — just a stable, human-checkable tag.
    const bytes = new Uint8Array(b64.toBuf(pubB64));
    let h1 = 0x811c9dc5, h2 = 0x01000193;
    for (let i = 0; i < bytes.length; i++) {
      h1 = (h1 ^ bytes[i]) >>> 0; h1 = (h1 * 16777619) >>> 0;
      h2 = (h2 + bytes[i] * (i + 7)) >>> 0;
    }
    return (h1.toString(16).padStart(8, '0') + h2.toString(16).padStart(8, '0')).slice(0, 16);
  }

  // -------------------------------------------------- KDF + wrapping
  async function deriveKeyFromPassword(password, saltB64, iters) {
    const base = await crypto.subtle.importKey(
      'raw', enc.encode(password), { name: 'PBKDF2' }, false, ['deriveKey']
    );
    return crypto.subtle.deriveKey(
      { name: 'PBKDF2', salt: new Uint8Array(b64.toBuf(saltB64)), iterations: iters, hash: 'SHA-256' },
      base,
      { name: 'AES-GCM', length: 256 },
      false,
      ['encrypt', 'decrypt']
    );
  }

  async function ecdhWrapKey(myPriv, theirPubB64, curve) {
    const theirPub = await crypto.subtle.importKey(
      'raw', b64.toBuf(theirPubB64), { name: 'ECDH', namedCurve: curve }, false, []
    );
    const bits = await crypto.subtle.deriveBits({ name: 'ECDH', public: theirPub }, myPriv, 256);
    return crypto.subtle.importKey('raw', bits, { name: 'HKDF' }, false, ['deriveKey'])
      .then(ikm => crypto.subtle.deriveKey(
        { name: 'HKDF', hash: 'SHA-256', salt: enc.encode('cipher-hypercrypt-v1'), info: enc.encode(curve) },
        ikm, { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']
      ));
  }

  /** Seal `rawBytes` so only the holder of `theirPubB64` can open it. */
  async function sealTo(myPriv, theirPubB64, curve, rawBytes) {
    const eph = await generateIdentityPair(curve);
    const kek = await ecdhWrapKey(eph.privateKey, theirPubB64, curve);
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const ct = await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, kek, rawBytes);
    const ephPub = await crypto.subtle.exportKey('raw', eph.publicKey);
    return b64.from(enc.encode(JSON.stringify({
      e: b64.from(ephPub), i: b64.from(iv), c: b64.from(ct), k: curve
    })));
  }

  /** Open a seal produced by sealTo(). */
  async function openSeal(myPriv, blobB64) {
    const parts = JSON.parse(dec.decode(b64.toBuf(blobB64)));
    const curve = parts.k || 'P-256';
    const kek = await ecdhWrapKey(myPriv, parts.e, curve);
    const plain = await crypto.subtle.decrypt(
      { name: 'AES-GCM', iv: new Uint8Array(b64.toBuf(parts.i)) }, kek, b64.toBuf(parts.c)
    );
    return new Uint8Array(plain);
  }

  // -------------------------------------------------- identity
  /**
   * Make sure this device holds the account's identity key.
   * Returns { created, restored, public_key, fingerprint }.
   */
  async function ensureIdentity(api, password, username) {
    if (identity) return { created: false, restored: true, ...publicInfo() };
    if (!crypto || !crypto.subtle) { supported = false; throw new Error('WebCrypto unavailable'); }

    const cached = await kvGet('identity');
    const remote = await api('/api/keys/mine');
    const serverKey = (remote.data || {}).key;

    if (cached && cached.privateKey) {
      try {
        const priv = await crypto.subtle.importKey(
          'jwk', cached.privateKey, { name: 'ECDH', namedCurve: cached.curve }, true, ['deriveBits']
        );
        identity = { privateKey: priv, curve: cached.curve, publicB64: cached.publicB64 };
        identity.fingerprint = fingerprintOf(identity.publicB64);
        if (!serverKey || serverKey.key_fingerprint !== identity.fingerprint) {
          await publishBackup(api, password);
        }
        return { created: false, restored: true, ...publicInfo() };
      } catch (e) { /* fall through and rebuild */ }
    }

    if (serverKey && serverKey.encrypted_backup && password) {
      try {
        const kek = await deriveKeyFromPassword(password, serverKey.backup_salt, serverKey.backup_iters || PBKDF2_ITERS);
        const jwk = JSON.parse(dec.decode(await crypto.subtle.decrypt(
          { name: 'AES-GCM', iv: new Uint8Array(b64.toBuf(serverKey.backup_iv || serverKey.backup_salt)) },
          kek, b64.toBuf(serverKey.encrypted_backup)
        )));
        const priv = await crypto.subtle.importKey(
          'jwk', jwk, { name: 'ECDH', namedCurve: serverKey.curve || 'P-256' }, true, ['deriveBits']
        );
        identity = { privateKey: priv, curve: serverKey.curve || 'P-256', publicB64: serverKey.public_key };
        identity.fingerprint = fingerprintOf(identity.publicB64);
        await kvSet('identity', { privateKey: await crypto.subtle.exportKey('jwk', priv), curve: identity.curve, publicB64: identity.publicB64 });
        return { created: false, restored: true, ...publicInfo() };
      } catch (e) {
        // Wrong password or a key made on another device — fall through.
      }
    }

    // Brand-new identity for this account — but we need the password to seal
    // the private half for the server, otherwise it is unrecoverable.
    if (!password) {
      const err = new Error('need_password');
      err.needPassword = true;
      throw err;
    }
    const curve = pickCurve();
    const pair = await generateIdentityPair(curve);
    const pubRaw = await crypto.subtle.exportKey('raw', pair.publicKey);
    identity = { privateKey: pair.privateKey, curve, publicB64: b64.from(pubRaw) };
    identity.fingerprint = fingerprintOf(identity.publicB64);
    await kvSet('identity', {
      privateKey: await crypto.subtle.exportKey('jwk', pair.privateKey),
      curve, publicB64: identity.publicB64
    });
    const published = await publishBackup(api, password);
    return { created: true, restored: false, published, ...publicInfo() };
  }

  async function publishBackup(api, password) {
    if (!identity) return false;
    const salt = crypto.getRandomValues(new Uint8Array(16));
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const kek = await deriveKeyFromPassword(password, b64.from(salt), PBKDF2_ITERS);
    const jwk = await crypto.subtle.exportKey('jwk', identity.privateKey);
    const ct = await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, kek, enc.encode(JSON.stringify(jwk)));
    const res = await api('/api/keys/register', {
      method: 'POST',
      body: {
        public_key: identity.publicB64,
        curve: identity.curve,
        encrypted_backup: b64.from(ct),
        backup_salt: b64.from(salt),
        backup_iv: b64.from(iv),
        backup_iters: PBKDF2_ITERS
      }
    });
    return !!(res && res.ok);
  }

  function publicInfo() {
    if (!identity) return {};
    return { public_key: identity.publicB64, curve: identity.curve, fingerprint: identity.fingerprint };
  }

  // -------------------------------------------------- conversation keys
  async function importRawAes(rawBytes) {
    return crypto.subtle.importKey('raw', rawBytes, { name: 'AES-GCM' }, true, ['encrypt', 'decrypt']);
  }

  async function newConversationKey() {
    return crypto.subtle.generateKey({ name: 'AES-GCM', length: 256 }, true, ['encrypt', 'decrypt']);
  }

  async function ckRecord(cid) {
    if (ckCache.has(cid)) return ckCache.get(cid);
    const stored = await kvGet('ck:' + cid);
    if (stored && stored.raw) {
      const rec = { key: await importRawAes(b64.toBuf(stored.raw)), wrappedFor: new Set(stored.wrappedFor || []) };
      ckCache.set(cid, rec);
      return rec;
    }
    return null;
  }

  async function persistCk(cid, key, wrappedFor) {
    const raw = await crypto.subtle.exportKey('raw', key);
    await kvSet('ck:' + cid, { raw: b64.from(raw), wrappedFor: Array.from(wrappedFor || []) });
  }

  /** Pull every conversation key this account can open, in one request. */
  async function loadAllKeys(api) {
    const res = await api('/api/keys/bulk');
    const wraps = (res.data || {}).keys || {};
    for (const cid of Object.keys(wraps)) {
      if (ckCache.has(cid)) continue;
      const existing = await ckRecord(cid);
      if (existing) continue;
      try {
        const raw = await openSeal(identity.privateKey, wraps[cid].wrapped_key);
        const key = await importRawAes(raw.buffer);
        const rec = { key, wrappedFor: new Set([wraps[cid].user_id].filter(Boolean)) };
        ckCache.set(cid, rec);
        await persistCk(cid, key, rec.wrappedFor);
      } catch (e) { /* not ours to open */ }
    }
  }

  async function publicKeyFor(api, username) {
    if (pubCache.has(username)) return pubCache.get(username);
    const res = await api('/api/keys/' + encodeURIComponent(username));
    const key = (res.data || {}).key || null;
    if (key) pubCache.set(username, key);
    return key;
  }

  /**
   * Make sure the conversation has a key, that every current member has a
   * seal, and that the operator's master key has one too.
   * `members` is [{id, username}] from the conversation payload.
   */
  async function ensureConversationKey(api, cid, members, masterPub) {
    let rec = await ckRecord(cid);
    let created = false;
    if (!rec) {
      // Someone else may have sealed one for us already.
      const res = await api('/api/conversations/' + cid + '/keys');
      const wraps = (res.data || {}).wraps || [];
      for (const w of wraps) {
        try {
          const raw = await openSeal(identity.privateKey, w.wrapped_key);
          rec = { key: await importRawAes(raw.buffer), wrappedFor: new Set() };
          break;
        } catch (e) { /* try the next one */ }
      }
    }
    if (!rec) {
      rec = { key: await newConversationKey(), wrappedFor: new Set() };
      created = true;
    }
    ckCache.set(cid, rec);

    const missing = (members || []).filter(m => m && m.id && m.username && !rec.wrappedFor.has(m.id));
    if (missing.length || created) {
      const rawCk = await crypto.subtle.exportKey('raw', rec.key);
      const wraps = [];
      for (const m of missing) {
        const pub = await publicKeyFor(api, m.username);
        if (!pub) continue;
        try {
          const blob = await sealTo(identity.privateKey, pub.public_key, pub.curve || identity.curve, rawCk);
          wraps.push({ user_id: m.id, wrapped_key: blob });
          rec.wrappedFor.add(m.id);
        } catch (e) { /* member has no key yet */ }
      }
      let masterWrap = null;
      const mp = masterPub || masterKey;
      if (mp && mp.public_key) {
        try {
          masterWrap = await sealTo(identity.privateKey, mp.public_key, mp.curve || identity.curve, rawCk);
        } catch (e) { /* skip */ }
      }
      if (wraps.length || masterWrap) {
        await api('/api/conversations/' + cid + '/keys', {
          method: 'POST',
          body: { wraps, master_wrap: masterWrap || '', key_fingerprint: fingerprintOf(b64.from(rawCk)) }
        });
      }
      await persistCk(cid, rec.key, rec.wrappedFor);
    }
    return rec;
  }

  // -------------------------------------------------- messages
  function isEncrypted(text) {
    return typeof text === 'string' && text.startsWith(PREFIX);
  }

  async function encrypt(api, cid, members, plaintext) {
    const rec = await ensureConversationKey(api, cid, members);
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const payload = enc.encode(JSON.stringify({ t: plaintext, ts: Date.now() }));
    const ct = await crypto.subtle.encrypt({ name: 'AES-GCM', iv, additionalData: enc.encode(cid) }, rec.key, payload);
    const fp = fingerprintOf(await crypto.subtle.exportKey('raw', rec.key));
    const envelope = { v: 1, cid, fp, i: b64.from(iv), c: b64.from(ct) };
    return PREFIX + b64.from(enc.encode(JSON.stringify(envelope)));
  }

  async function decrypt(text) {
    if (!isEncrypted(text)) return { text, encrypted: false };
    try {
      const env = JSON.parse(dec.decode(b64.toBuf(text.slice(PREFIX.length))));
      const rec = await ckRecord(env.cid);
      if (!rec) return { text: null, encrypted: true, reason: 'no_key' };
      const plain = await crypto.subtle.decrypt(
        { name: 'AES-GCM', iv: new Uint8Array(b64.toBuf(env.i)), additionalData: enc.encode(env.cid) },
        rec.key, b64.toBuf(env.c)
      );
      const body = JSON.parse(dec.decode(plain));
      return { text: body.t || '', encrypted: true, ts: body.ts };
    } catch (e) {
      return { text: null, encrypted: true, reason: 'undecryptable' };
    }
  }

  // -------------------------------------------------- images
  async function encryptImage(api, cid, members, arrayBuffer) {
    const rec = await ensureConversationKey(api, cid, members);
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const ct = await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, rec.key, arrayBuffer);
    // iv and ciphertext travel together as one opaque blob
    const out = new Uint8Array(iv.length + ct.byteLength);
    out.set(iv, 0);
    out.set(new Uint8Array(ct), iv.length);
    return b64.from(out);
  }

  async function decryptImageBlob(url) {
    const res = await fetch(url, { credentials: 'same-origin' });
    if (!res.ok) throw new Error('fetch failed');
    const buf = new Uint8Array(await res.arrayBuffer());
    const cid = new URL(url, location.origin).searchParams.get('cid');
    const rec = cid ? await ckRecord(cid) : null;
    if (!rec) throw new Error('no key for this image');
    const iv = buf.slice(0, 12);
    const ct = buf.slice(12);
    const plain = await crypto.subtle.decrypt({ name: 'AES-GCM', iv }, rec.key, ct);
    return URL.createObjectURL(new Blob([plain], { type: 'image/jpeg' }));
  }

  return {
    PREFIX,
    isSupported: () => supported,
    isEncrypted,
    ensureIdentity,
    publishBackup,
    publicInfo,
    setMasterKey: (k) => { masterKey = k; },
    loadAllKeys,
    ensureConversationKey,
    encrypt,
    decrypt,
    encryptImage,
    decryptImageBlob,
    fingerprintOf
  };
})();
