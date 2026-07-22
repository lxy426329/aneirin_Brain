if (!CanvasRenderingContext2D.prototype.roundRect) {
  CanvasRenderingContext2D.prototype.roundRect = function(x, y, w, h, r) {
    if (w < 2 * r) r = w / 2;
    if (h < 2 * r) r = h / 2;
    this.beginPath();
    this.moveTo(x + r, y);
    this.arcTo(x + w, y, x + w, y + h, r);
    this.arcTo(x + w, y + h, x, y + h, r);
    this.arcTo(x, y + h, x, y, r);
    this.arcTo(x, y, x + w, y, r);
    this.closePath();
    return this;
  };
}

// ========================================
// Auth system / 认证系统
// ========================================
async function checkAuth() {
  try {
    const resp = await fetch('/auth/status');
    const data = await resp.json();
    if (data.setup_needed) {
      document.getElementById('auth-subtitle').textContent = '首次设置';
      document.getElementById('auth-setup-form').style.display = 'block';
      return false;
    } else if (data.authenticated) {
      document.getElementById('auth-overlay').style.display = 'none';
      return true;
    } else {
      document.getElementById('auth-subtitle').textContent = '请输入访问密码';
      document.getElementById('auth-login-form').style.display = 'block';
      return false;
    }
  } catch {
    document.getElementById('auth-overlay').style.display = 'none';
    return true;
  }
}

function showAuthError(msg) {
  const el = document.getElementById('auth-error');
  el.textContent = msg;
  el.style.display = 'block';
}

async function doSetup() {
  const p1 = document.getElementById('auth-setup-pwd').value;
  const p2 = document.getElementById('auth-setup-pwd2').value;
  if (p1.length < 6) return showAuthError('密码至少6位');
  if (p1 !== p2) return showAuthError('两次密码不一致');
  const resp = await fetch('/auth/setup', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({password: p1}) });
  if (resp.ok) {
    document.getElementById('auth-overlay').style.display = 'none';
    document.getElementById('auth-setup-form').style.display = 'none';
    loadBuckets();
  } else {
    const d = await resp.json();
    showAuthError(d.detail || '设置失败');
  }
}

let loginAbortController = null;

async function doLogin() {
  if (loginAbortController) {
    loginAbortController.abort();
  }
  
  const pwd = document.getElementById('auth-login-pwd').value;
  loginAbortController = new AbortController();
  
  try {
    const resp = await fetch('/auth/login', { 
      method: 'POST', 
      headers: {'Content-Type':'application/json'}, 
      body: JSON.stringify({password: pwd}),
      signal: loginAbortController.signal
    });
    
    if (resp.ok) {
      document.getElementById('auth-overlay').style.display = 'none';
      document.getElementById('auth-login-form').style.display = 'none';
      loadBuckets();
      checkAIStatus();
    } else {
      const d = await resp.json();
      showAuthError(d.detail || '密码错误');
    }
  } catch (e) {
    if (e.name !== 'AbortError') {
      console.warn('登录请求失败:', e.message);
    }
  } finally {
    loginAbortController = null;
  }
}

async function doLogout() {
  await fetch('/auth/logout', { method: 'POST' });
  document.getElementById('auth-setup-form').style.display = 'none';
  document.getElementById('auth-login-form').style.display = 'none';
  document.getElementById('auth-login-form').style.display = 'block';
  document.getElementById('auth-subtitle').textContent = '请输入访问密码';
  document.getElementById('auth-error').style.display = 'none';
  document.getElementById('auth-overlay').style.display = 'flex';
}

async function changePassword() {
  const currentPwd = document.getElementById('settings-current-pwd').value;
  const newPwd = document.getElementById('settings-new-pwd').value;
  const newPwd2 = document.getElementById('settings-new-pwd2').value;
  const msgEl = document.getElementById('settings-pwd-msg');
  if (newPwd.length < 6) { msgEl.style.color = 'var(--negative)'; msgEl.textContent = '新密码至少6位'; return; }
  if (newPwd !== newPwd2) { msgEl.style.color = 'var(--negative)'; msgEl.textContent = '两次密码不一致'; return; }
  const resp = await authFetch('/auth/change-password', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({current: currentPwd, new: newPwd}) });
  if (!resp) return;
  if (resp.ok) {
    msgEl.style.color = 'var(--accent)'; msgEl.textContent = '密码修改成功';
    document.getElementById('settings-current-pwd').value = '';
    document.getElementById('settings-new-pwd').value = '';
    document.getElementById('settings-new-pwd2').value = '';
  } else {
    const d = await resp.json();
    msgEl.style.color = 'var(--negative)'; msgEl.textContent = d.detail || '修改失败';
  }
}

async function loadSettingsStatus() {
  const el = document.getElementById('settings-status');
  try {
    const resp = await authFetch('/api/status');
    if (!resp) return;
    const d = await resp.json();
    const noticeEl = document.getElementById('settings-env-notice');
    if (d.using_env_password) noticeEl.style.display = 'block';
    else noticeEl.style.display = 'none';
    el.innerHTML = `
      <b>版本</b>：${d.version}<br>
      <b>Bucket 总数</b>：${(d.buckets?.total ?? 0)} （永久:${d.buckets?.permanent ?? 0} / 动态:${d.buckets?.dynamic ?? 0} / 归档:${d.buckets?.archive ?? 0}）<br>
      <b>衰减引擎</b>：${d.decay_engine}<br>
      <b>向量搜索</b>：${d.embedding_enabled ? '已启用' : '未启用'}<br>
    `;
  } catch(e) {
    el.textContent = '加载失败: ' + e;
  }
  // Also refresh the host-vault input whenever the settings tab is loaded.
  loadHostVault();
}

async function loadHostVault() {
  const input = document.getElementById('settings-host-vault');
  const msg = document.getElementById('settings-host-vault-msg');
  if (!input) return;
  msg.textContent = '';
  msg.style.color = 'var(--text-dim)';
  try {
    const resp = await authFetch('/api/host-vault');
    if (!resp) return;
    const d = await resp.json();
    input.value = d.value || '';
    if (d.source === 'env') {
      msg.textContent = '当前由进程环境变量提供（修改 .env 不会立即覆盖）';
      msg.style.color = 'var(--warning)';
    } else if (d.source === 'file') {
      msg.textContent = '当前来自 ' + (d.env_file || '.env');
    } else {
      msg.textContent = '尚未设置（默认使用 ./buckets）';
    }
  } catch(e) {
    msg.style.color = 'var(--negative)';
    msg.textContent = '加载失败: ' + e;
  }
}

async function saveHostVault() {
  const input = document.getElementById('settings-host-vault');
  const msg = document.getElementById('settings-host-vault-msg');
  if (!input) return;
  const value = input.value.trim();
  msg.textContent = '保存中…';
  msg.style.color = 'var(--text-dim)';
  try {
    const resp = await authFetch('/api/host-vault', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({value})
    });
    if (!resp) return;
    const d = await resp.json();
    if (resp.ok) {
      msg.style.color = 'var(--accent)';
      msg.textContent = '已保存 → ' + (d.env_file || '.env') + '（需重启容器生效）';
    } else {
      msg.style.color = 'var(--negative)';
      msg.textContent = d.error || '保存失败';
    }
  } catch(e) {
    msg.style.color = 'var(--negative)';
    msg.textContent = '保存失败: ' + e;
  }
}

// authFetch: wraps fetch, shows auth overlay on 401
async function authFetch(url, options) {
  const resp = await fetch(url, options);
  if (resp.status === 401) {
    doLogout();
    return null;
  }
  return resp;
}

// ========================================

const BASE = location.origin;
let allBuckets = [];
let currentFilter = 'all';

const apiCache = {
  buckets: { data: null, timestamp: 0, ttl: 60000 },
  directory: { data: null, timestamp: 0, ttl: 120000 },
  experiences: { data: null, timestamp: 0, ttl: 60000 },
  anchors: { data: null, timestamp: 0, ttl: 60000 },
  identities: { data: null, timestamp: 0, ttl: 60000 },
  patterns: { data: null, timestamp: 0, ttl: 120000 },
  timelines: { data: null, timestamp: 0, ttl: 60000 },
  candlesticks: { data: null, timestamp: 0, ttl: 60000 },
};

function getCachedData(key) {
  const cache = apiCache[key];
  if (cache && cache.data && Date.now() - cache.timestamp < cache.ttl) {
    return cache.data;
  }
  return null;
}

function setCachedData(key, data) {
  if (apiCache[key]) {
    apiCache[key] = { data, timestamp: Date.now(), ttl: apiCache[key].ttl };
  }
}

function invalidateCache(key) {
  if (apiCache[key]) {
    apiCache[key].data = null;
    apiCache[key].timestamp = 0;
  }
}

const PLUTCHIK_EMOTIONS = {
  '愤怒': { color: '#F44336', intensity: ['烦恼', '生气', '愤怒', '暴怒'], opposite: '信任' },
  '恐惧': { color: '#E91E63', intensity: ['不安', '焦虑', '害怕', '恐惧'], opposite: '喜悦' },
  '悲伤': { color: '#9C27B0', intensity: ['忧伤', '悲伤', '悲痛', '绝望'], opposite: '期待' },
  '厌恶': { color: '#795548', intensity: ['不悦', '反感', '厌恶', '憎恨'], opposite: '惊讶' },
  '惊讶': { color: '#00BCD4', intensity: ['好奇', '惊讶', '震惊', '惊愕'], opposite: '厌恶' },
  '期待': { color: '#FFC107', intensity: ['期待', '希望', '兴奋', '狂喜'], opposite: '悲伤' },
  '信任': { color: '#4CAF50', intensity: ['接受', '信任', '热爱', '迷恋'], opposite: '愤怒' },
  '喜悦': { color: '#FF5722', intensity: ['满意', '快乐', '喜悦', '幸福'], opposite: '恐惧' }
};

const EMOTION_COLORS = {};
Object.keys(PLUTCHIK_EMOTIONS).forEach(base => {
  PLUTCHIK_EMOTIONS[base].intensity.forEach((label, idx) => {
    const color = PLUTCHIK_EMOTIONS[base].color;
    const alpha = ['40', '50', '60', '70'][idx];
    EMOTION_COLORS[label] = color;
  });
});

function getEmotionColor(label) {
  return EMOTION_COLORS[label] || '#90A4AE';
}

const GENERIC_TAGS = ['工作', '学习', '生活', '健康', '人际关系', '兴趣爱好', '财务', '内心世界', '数字技术', '事务管理', '休闲娱乐', '家庭', '情感', '成长', '创造'];

function buildTagDisplay(tags) {
  if (!tags || tags.length === 0) return '—';
  
  var genericTags = tags.filter(t => GENERIC_TAGS.includes(t));
  var specificTags = tags.filter(t => !GENERIC_TAGS.includes(t));
  
  var html = '';
  if (genericTags.length > 0) {
    html += '<div style="margin-bottom:4px;">';
    html += '<span style="font-size:10px;color:var(--text-light);margin-right:4px;">泛化标签:</span>';
    html += genericTags.map(t => '<span style="background:var(--accent);color:white;padding:2px 8px;border-radius:10px;font-size:11px;margin-right:4px;">' + esc(t) + '</span>').join('');
    html += '</div>';
  }
  if (specificTags.length > 0) {
    html += '<div>';
    html += '<span style="font-size:10px;color:var(--text-light);margin-right:4px;">具体标签:</span>';
    html += specificTags.map(t => '<span style="background:var(--border);color:var(--text);padding:2px 8px;border-radius:10px;font-size:11px;margin-right:4px;">' + esc(t) + '</span>').join('');
    html += '</div>';
  }
  
  return html;
}

document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    const target = tab.dataset.tab;
    document.getElementById('list-view').style.display = target === 'list' ? '' : 'none';
    document.getElementById('directory-view').style.display = target === 'directory' ? '' : 'none';
    document.getElementById('experience-view').style.display = target === 'experience' ? '' : 'none';
    document.getElementById('anchor-view').style.display = target === 'anchor' ? '' : 'none';
    document.getElementById('identity-view').style.display = target === 'identity' ? '' : 'none';
    document.getElementById('timeline-view').style.display = target === 'timeline' ? '' : 'none';
    document.getElementById('candlestick-view').style.display = target === 'candlestick' ? '' : 'none';
    document.getElementById('network-view').style.display = target === 'network' ? '' : 'none';
    document.getElementById('config-view').style.display = target === 'config' ? '' : 'none';
    if (target === 'network') loadNetwork();
    if (target === 'config') loadConfig();
    if (target === 'identity') loadIdentities();
    if (target === 'directory') loadDirectory(true);
    if (target === 'experience') loadExperiences();
    if (target === 'anchor') loadAnchors();
    if (target === 'timeline') loadTimelines();
    if (target === 'candlestick') loadCandlesticks();
  });
});

async function loadBuckets() {
  try {
    const cached = getCachedData('buckets');
    if (cached) {
      allBuckets = cached;
      updateStats();
      buildFilters();
      renderBuckets(allBuckets);
      return;
    }
    
    const res = await fetch(BASE + '/api/buckets');
    const data = await res.json();
    if (!res.ok) {
      throw new Error((data && data.error) ? data.error : `HTTP ${res.status}`);
    }
    const buckets = data.buckets || data;
    allBuckets = buckets;
    setCachedData('buckets', buckets);
    updateStats();
    buildFilters();
    renderBuckets(allBuckets);
  } catch (e) {
    document.getElementById('bucket-list').innerHTML = '<div class="loading">加载失败: ' + e.message + '</div>';
  }
}

function updateStats() {
  const total = allBuckets.length;
  const pinned = allBuckets.filter(b => b.pinned).length;
  const feels = allBuckets.filter(b => b.type === 'feel').length;
  const identities = allBuckets.filter(b => b.type === 'identity').length;
  const patterns = allBuckets.filter(b => b.type === 'pattern').length;
  const events = allBuckets.filter(b => !b.type || b.type === 'event').length;
  const resolved = allBuckets.filter(b => b.resolved).length;
  const digested = allBuckets.filter(b => b.digested).length;
  
  const emotionCounts = {};
  allBuckets.forEach(b => {
    if (b.emotions && Array.isArray(b.emotions)) {
      b.emotions.forEach(e => {
        emotionCounts[e.label] = (emotionCounts[e.label] || 0) + 1;
      });
    }
  });
  const topEmotions = Object.entries(emotionCounts).sort((a, b) => b[1] - a[1]).slice(0, 3);
  const emotionStr = topEmotions.length > 0 ? ' · ' + topEmotions.map(e => e[0] + ':' + e[1]).join(' ') : '';
  
  document.getElementById('stats').innerHTML =
    '<div style="display:flex;gap:16px;align-items:center;">' +
    '<span style="font-weight:600;color:var(--text);">' + total + ' 记忆</span>' +
    '<span style="color:#4A7C59;">' + identities + '</span>' +
    '<span style="color:#6A6A8B;">' + patterns + '</span>' +
    '<span style="color:#2F4F4F;">' + events + '</span>' +
    '<span style="color:#9A7B4F;">' + pinned + '</span>' +
    '<span style="color:#8B6A6A;">' + feels + '</span>' +
    '<span style="color:#81C784;">' + resolved + '</span>' +
    '<span style="color:#FFB74D;">' + digested + '</span>' +
    (emotionStr ? '<span style="color:var(--text-dim);font-size:12px;">' + emotionStr + '</span>' : '') +
    '</div>';
}

function buildFilters() {
  const domains = new Set();
  allBuckets.forEach(b => (b.domain || []).forEach(d => domains.add(d)));
  const filters = document.getElementById('filters');
  const types = [
    { key: 'all', label: '全部' },
    { key: 'identity', label: '身份' },
    { key: 'pattern', label: '模式' },
    { key: 'pinned', label: '钉选' },
    { key: 'feel', label: 'Feel' },
    { key: 'unresolved', label: '未解决' },
    { key: 'digested', label: '已消化' },
    { key: 'archived', label: '归档' },
  ];
  filters.innerHTML = types.map(function(t) {
    return '<button class="filter-btn ' + (t.key === 'all' ? 'active' : '') + '" data-filter="' + t.key + '">' + t.label + '</button>';
  }).join('') + Array.from(domains).slice(0, 10).map(function(d) {
    return '<button class="filter-btn" data-filter="domain:' + d + '">' + d + '</button>';
  }).join('');
}

document.getElementById('filters').addEventListener('click', function(e) {
  var btn = e.target.closest('.filter-btn');
  if (!btn) return;
  document.getElementById('filters').querySelectorAll('.filter-btn').forEach(function(b) { b.classList.remove('active'); });
  btn.classList.add('active');
  currentFilter = btn.dataset.filter;
  renderBuckets(filterBuckets(allBuckets));
});

document.getElementById('bucket-list').addEventListener('click', function(e) {
  var row = e.target.closest('.bucket-row');
  if (!row) return;
  var bucketId = row.dataset.bucketId;
  if (bucketId) showDetail(bucketId);
});

document.getElementById('anchor-list').addEventListener('click', function(e) {
  var viewBtn = e.target.closest('.btn-view-bucket');
  if (viewBtn) {
    showDetail(viewBtn.dataset.bucketId);
    return;
  }
  var deleteBtn = e.target.closest('.btn-delete-anchor');
  if (deleteBtn) {
    deleteAnchor(deleteBtn.dataset.anchorId);
    return;
  }
});

function filterBuckets(buckets) {
  if (currentFilter === 'all') return buckets;
  if (currentFilter === 'identity') return buckets.filter(function(b) { return b.type === 'identity'; });
  if (currentFilter === 'pattern') return buckets.filter(function(b) { return b.type === 'pattern'; });
  if (currentFilter === 'pinned') return buckets.filter(function(b) { return b.pinned; });
  if (currentFilter === 'feel') return buckets.filter(function(b) { return b.type === 'feel'; });
  if (currentFilter === 'unresolved') return buckets.filter(function(b) { return !b.resolved && b.type !== 'permanent' && !b.pinned; });
  if (currentFilter === 'digested') return buckets.filter(function(b) { return b.digested; });
  if (currentFilter === 'archived') return buckets.filter(function(b) { return b.type === 'archived' || b.score < 0.3; });
  if (currentFilter.startsWith('domain:')) {
    var d = currentFilter.slice(7);
    return buckets.filter(function(b) { return (b.domain || []).includes(d); });
  }
  return buckets;
}

function renderBuckets(buckets) {
  var list = document.getElementById('bucket-list');
  if (!buckets || !buckets.length) {
    list.innerHTML = '<div class="loading">没有记忆桶</div>';
    return;
  }
  try {
    var html = '';
    for (var i = 0; i < buckets.length; i++) {
      var b = buckets[i];
      var icon = '';
      var bucketType = b.type || 'event';
      
      var emotionDisplay = '';
      if (b.emotions && Array.isArray(b.emotions) && b.emotions.length > 0) {
        for (var j = 0; j < Math.min(b.emotions.length, 3); j++) {
          var e = b.emotions[j];
          var emotionColor = getEmotionColor(e.label);
          var intensityAlpha = e.intensity > 0.7 ? '40' : e.intensity > 0.4 ? '30' : '20';
          emotionDisplay += '<span style="display:inline-flex;align-items:center;gap:4px;margin-right:6px;">' +
            '<span style="background:' + emotionColor + intensityAlpha + ';color:' + emotionColor + ';padding:2px 6px;border-radius:8px;font-size:11px;">' + esc(e.label) + '</span>' +
            '<span style="width:30px;height:4px;background:var(--border);border-radius:2px;overflow:hidden;display:inline-block;">' +
              '<span style="display:block;height:100%;background:' + emotionColor + ';width:' + (e.intensity * 100) + '%"></span>' +
            '</span>' +
          '</span>';
        }
      } else {
        emotionDisplay = 'V' + (b.valence || 0.5).toFixed(1) + '/A' + (b.arousal || 0.3).toFixed(1);
      }
      
      var shortId = b.id.substring(0, 8);
      html += '<div class="bucket-row' + (b.type === 'identity' ? ' identity-card' : b.type === 'pattern' ? ' pattern-card' : '') + '" data-bucket-id="' + b.id + '">' +
        '<span class="name" title="' + esc(b.name) + '">' + esc(b.name) + '<span style="color:var(--text-light);font-size:11px;margin-left:6px;font-weight:400;">#' + shortId + '</span></span>' +
        '<span class="type">' + bucketType + '</span>' +
        '<span class="domain">' + (b.domain || []).join(', ') + '</span>' +
        '<span class="emotion">' + emotionDisplay + '</span>' +
        '<span class="score">' + (b.score || 0).toFixed(2) + '</span>' +
      '</div>';
    }
    list.innerHTML = html;
  } catch (e) {
    console.error('renderBuckets failed:', e);
    list.innerHTML = '<div class="loading">渲染失败</div>';
  }
}

async function searchBuckets(query) {
  try {
    var res = await fetch(BASE + '/api/search?q=' + encodeURIComponent(query));
    var results = await res.json();
    renderBuckets(results);
  } catch (e) {
    console.error('Search failed:', e);
  }
}

async function showDetail(id) {
  var panel = document.getElementById('detail-panel');
  var content = document.getElementById('detail-content');
  content.innerHTML = '<div class="loading">加载中…</div>';
  panel.classList.add('open');

  try {
    var res = await fetch(BASE + '/api/bucket/' + id);
    var b = await res.json();
    var meta = b.metadata || {};
    var bucketType = meta.type || 'event';
    
    // Build emotion display
    var emotionHtml = '';
    if (meta.emotions && Array.isArray(meta.emotions) && meta.emotions.length > 0) {
      emotionHtml = '<div style="display:flex;flex-direction:column;gap:8px;">';
      meta.emotions.forEach(function(e, index) {
        var emotionColor = getEmotionColor(e.label);
        var intensityPercent = (e.intensity * 100).toFixed(0);
        
        var polarityBadge = '';
        if (e.polarity === 'positive') polarityBadge = '<span style="background:#E8F5E9;color:#2E7D32;padding:1px 6px;border-radius:6px;font-size:9px;">+</span>';
        else if (e.polarity === 'negative') polarityBadge = '<span style="background:#FFEBEE;color:#C62828;padding:1px 6px;border-radius:6px;font-size:9px;">-</span>';
        else polarityBadge = '<span style="background:#ECEFF1;color:#546E7A;padding:1px 6px;border-radius:6px;font-size:9px;">~</span>';
        
        var arousalBadge = '';
        if (e.arousal_level === 'high') arousalBadge = '<span style="background:#FFF3E0;color:#E65100;padding:1px 6px;border-radius:6px;font-size:9px;">高唤醒</span>';
        else if (e.arousal_level === 'medium') arousalBadge = '<span style="background:#E3F2FD;color:#1565C0;padding:1px 6px;border-radius:6px;font-size:9px;">中唤醒</span>';
        else arousalBadge = '<span style="background:#F3E5F5;color:#6A1B9A;padding:1px 6px;border-radius:6px;font-size:9px;">低唤醒</span>';
        
        var durationBadge = '';
        if (e.duration === 'long') durationBadge = '<span style="background:#E8F5E9;color:#2E7D32;padding:1px 6px;border-radius:6px;font-size:9px;">长期</span>';
        else if (e.duration === 'short') durationBadge = '<span style="background:#FFF3E0;color:#E65100;padding:1px 6px;border-radius:6px;font-size:9px;">短期</span>';
        else durationBadge = '<span style="background:#ECEFF1;color:#546E7A;padding:1px 6px;border-radius:6px;font-size:9px;">瞬时</span>';
        
        emotionHtml += '<div style="display:flex;align-items:center;gap:8px;">' +
          '<span style="width:40px;font-size:12px;color:' + emotionColor + ';">' + esc(e.label) + '</span>' +
          polarityBadge +
          arousalBadge +
          durationBadge +
          '<span style="flex:1;height:8px;background:var(--border);border-radius:4px;overflow:hidden;">' +
            '<span style="display:block;height:100%;background:' + emotionColor + ';width:' + intensityPercent + '%;transition:width 0.3s;"></span>' +
          '</span>' +
          '<span style="width:35px;font-size:12px;color:var(--text-dim);text-align:right;">' + intensityPercent + '%</span>' +
        '</div>';
      });
      emotionHtml += '</div>';
      if (meta.dominant_emotion) {
        emotionHtml += '<div style="margin-top:8px;font-size:11px;color:var(--text-dim);">主情绪: <span style="color:var(--accent);">' + esc(meta.dominant_emotion) + '</span></div>';
      }
      if (meta.emotion_metrics) {
        var em = meta.emotion_metrics;
        emotionHtml += '<div style="margin-top:12px;padding:8px;background:var(--surface);border-radius:8px;">';
        emotionHtml += '<div style="font-size:11px;color:var(--text-light);margin-bottom:6px;">情绪综合指标</div>';
        emotionHtml += '<div style="display:flex;gap:12px;font-size:11px;">';
        emotionHtml += '<span>整体强度: <b>' + (em.overall_intensity || 0).toFixed(2) + '</b></span>';
        emotionHtml += '<span>波动范围: <b>' + (em.emotional_range || 0).toFixed(2) + '</b></span>';
        emotionHtml += '<span>情绪效价: <b>' + (em.emotional_valence || 0).toFixed(2) + '</b></span>';
        emotionHtml += '</div></div>';
      }
    }
    
    var detailHtml = '<h2>' + esc(meta.name || id) + '</h2>';
    
    // Add type-specific fields
    if (bucketType === 'identity') {
      detailHtml += '<div class="detail-meta">' +
        '<div class="field"><label>ID</label>' + id + '</div>' +
        '<div class="field"><label>类型</label>身份档案</div>' +
        '<div class="field"><label>别名</label>' + (meta.aliases || []).join(', ') + '</div>' +
        '<div class="field"><label>性格特征</label>' + (meta.core_traits || []).join(', ') + '</div>' +
        '<div class="field"><label>关系</label>' + (meta.relationships || []).join('<br>') + '</div>' +
        '<div class="field"><label>创建</label>' + (meta.created || '—') + '</div>' +
        '<div class="field"><label>最后活跃</label>' + (meta.last_active || '—') + '</div>' +
      '</div>';
      if (meta.basic_info && Object.keys(meta.basic_info).length > 0) {
        detailHtml += '<div style="margin-bottom:16px;"><label style="color:var(--text-light);font-size:11px;text-transform:uppercase;letter-spacing:0.5px;">基本信息</label>' +
          Object.entries(meta.basic_info).map(function(kv) { return '<div><b>' + esc(kv[0]) + ':</b> ' + esc(kv[1]); }).join('<br>') +
        '</div>';
      }
    } else if (bucketType === 'pattern') {
      detailHtml += '<div class="detail-meta">' +
        '<div class="field"><label>ID</label>' + id + '</div>' +
        '<div class="field"><label>类型</label>行为模式</div>' +
        '<div class="field"><label>置信度</label>' + ((meta.confidence || 0.5) * 100).toFixed(0) + '%</div>' +
        '<div class="field"><label>适用场景</label>' + (meta.applicable_scenes || []).join(', ') + '</div>' +
        '<div class="field"><label>激活次数</label>' + (meta.activation_count || 0) + '</div>' +
        '<div class="field"><label>创建</label>' + (meta.created || '—') + '</div>' +
      '</div>' +
      '<div style="margin-bottom:16px;"><label style="color:var(--text-light);font-size:11px;text-transform:uppercase;letter-spacing:0.5px;">规律描述</label><p style="margin:8px 0;">' + esc(meta.summary || '—') + '</p></div>';
      if (meta.source_events && meta.source_events.length > 0) {
        detailHtml += '<div style="margin-bottom:16px;"><label style="color:var(--text-light);font-size:11px;text-transform:uppercase;letter-spacing:0.5px;">来源事件</label>' +
          meta.source_events.map(function(e) { return '<code style="font-size:11px;">' + e + '</code>'; }).join(' ') +
        '</div>';
      }
    } else {
      // Event/feel bucket
      detailHtml += '<div class="detail-meta">' +
        '<div class="field"><label>ID</label>' + id + '</div>' +
        '<div class="field"><label>类型</label>' + bucketType + '</div>' +
        '<div class="field"><label>域</label>' + (meta.domain || []).join(', ') + '</div>' +
        '<div class="field"><label>标签</label>' + buildTagDisplay(meta.tags || []) + '</div>';
      
      if (emotionHtml) {
        detailHtml += '<div class="field"><label>情绪</label>' + emotionHtml + '</div>';
      } else {
        detailHtml += '<div class="field"><label>效价</label>V' + (meta.valence || 0.5).toFixed(2) + '</div>' +
          '<div class="field"><label>唤醒度</label>A' + (meta.arousal || 0.3).toFixed(2) + '</div>';
      }
      
      detailHtml += '<div class="field"><label>模型视角</label>' + (meta.model_valence != null ? 'V' + meta.model_valence.toFixed(2) : '—') + '</div>' +
        '<div class="field"><label>重要度</label>' + (meta.importance || 5) + '/10</div>';
      
      var impDetails = meta.importance_details || {};
      if (impDetails.impact > 0 || impDetails.duration > 0 || impDetails.emotional_intensity > 0 || impDetails.recurrence > 0 || impDetails.interconnectedness > 0) {
        detailHtml += '<div class="field"><label>重要度详情</label>' +
          '<div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:4px;">' +
          (impDetails.impact > 0 ? '<span style="background:#FFE0B2;padding:2px 8px;border-radius:10px;font-size:11px;">影响:' + impDetails.impact + '</span>' : '') +
          (impDetails.duration > 0 ? '<span style="background:#BBDEFB;padding:2px 8px;border-radius:10px;font-size:11px;">持续:' + impDetails.duration + '</span>' : '') +
          (impDetails.emotional_intensity > 0 ? '<span style="background:#F8BBD9;padding:2px 8px;border-radius:10px;font-size:11px;">情感:' + impDetails.emotional_intensity + '</span>' : '') +
          (impDetails.recurrence > 0 ? '<span style="background:#C8E6C9;padding:2px 8px;border-radius:10px;font-size:11px;">重复:' + impDetails.recurrence + '</span>' : '') +
          (impDetails.interconnectedness > 0 ? '<span style="background:#E1BEE7;padding:2px 8px;border-radius:10px;font-size:11px;">关联:' + impDetails.interconnectedness + '</span>' : '') +
          '</div></div>';
      }
      
      detailHtml += '<div class="field"><label>权重分</label>' + b.score.toFixed(4) + '</div>' +
        '<div class="field"><label>激活次数</label>' + (meta.activation_count || 1) + '</div>' +
        '<div class="field"><label>已解决</label>' + (meta.resolved ? '✓' : '—') + '</div>' +
        '<div class="field"><label>已消化</label>' + (meta.digested ? '✓' : '—') + '</div>' +
        '<div class="field"><label>钉选</label>' + (meta.pinned ? '✓' : '—') + '</div>' +
        '<div class="field"><label>创建</label>' + (meta.created || '—') + '</div>' +
        '<div class="field"><label>最后活跃</label>' + (meta.last_active || '—') + '</div>';
      
      if (meta.related_buckets && meta.related_buckets.length > 0) {
        detailHtml += '<div class="field"><label>关联记忆</label>' +
          '<div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:4px;">' +
          meta.related_buckets.map(id => '<span class="tag" onclick="loadBucketDetail(\'' + id + '\')" style="cursor:pointer;background:#ECEFF1;padding:2px 8px;border-radius:6px;font-size:11px;">' + id + '</span>').join('') +
          '</div></div>';
      }
      
      if (meta.parent_bucket) {
        detailHtml += '<div class="field"><label>父级记忆</label>' +
          '<span class="tag" onclick="loadBucketDetail(\'' + meta.parent_bucket + '\')" style="cursor:pointer;background:#BBDEFB;padding:2px 8px;border-radius:6px;font-size:11px;">' + meta.parent_bucket + '</span></div>';
      }
      
      if (meta.child_buckets && meta.child_buckets.length > 0) {
        detailHtml += '<div class="field"><label>子级记忆</label>' +
          '<div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:4px;">' +
          meta.child_buckets.map(id => '<span class="tag" onclick="loadBucketDetail(\'' + id + '\')" style="cursor:pointer;background:#C8E6C9;padding:2px 8px;border-radius:6px;font-size:11px;">' + id + '</span>').join('') +
          '</div></div>';
      }
      
      if (meta.event_sequence && meta.event_sequence.length > 0) {
        detailHtml += '<div class="field"><label>事件链</label>' +
          '<div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:4px;">' +
          meta.event_sequence.map((id, i) => '<span class="tag" onclick="loadBucketDetail(\'' + id + '\')" style="cursor:pointer;background:#FFF9C4;padding:2px 8px;border-radius:6px;font-size:11px;">#' + (i+1) + ' ' + id + '</span>').join(' → ') +
          '</div></div>';
      }
      
      detailHtml += '</div>';
    }
    
    detailHtml += '<div class="detail-content">' + esc(b.content) + '</div>';
    
    detailHtml += '<div style="margin-top:24px;padding-top:20px;border-top:1px solid var(--border);">';
    detailHtml += '<button onclick="showAddRelationModal(\'' + id + '\')" style="width:100%;padding:10px;border:none;background:var(--accent);color:white;border-radius:12px;cursor:pointer;font-size:13px;">+ 添加关联记忆</button>';
    detailHtml += '</div>';
    
    content.innerHTML = detailHtml;
  } catch (e) {
    content.innerHTML = '<div class="loading">加载失败: ' + e.message + '</div>';
  }
}

function showAddRelationModal(sourceId) {
  var modal = document.createElement('div');
  modal.className = 'modal-overlay';
  modal.style.display = 'flex';
  modal.innerHTML = `
    <div style="background:var(--surface-solid);border-radius:24px;padding:24px;width:480px;max-height:80vh;overflow-y:auto;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;">
        <h3 style="margin:0;">添加关联记忆</h3>
        <button onclick="this.closest('.modal-overlay').remove()" style="background:none;border:none;font-size:20px;cursor:pointer;color:var(--text-dim);">&times;</button>
      </div>
      <input type="text" id="relation-search-input" placeholder="搜索记忆桶名称..." style="width:100%;padding:10px;border-radius:12px;border:1px solid var(--border);margin-bottom:16px;" />
      <div id="relation-search-results" style="max-height:300px;overflow-y:auto;"></div>
      <div id="relation-add-msg" style="margin-top:12px;font-size:13px;"></div>
    </div>
  `;
  document.body.appendChild(modal);
  
  var searchInput = modal.querySelector('#relation-search-input');
  searchInput.focus();
  
  searchInput.addEventListener('input', function() {
    searchBucketsForRelation(sourceId, this.value);
  });
  
  searchBucketsForRelation(sourceId, '');
}

async function searchBucketsForRelation(sourceId, query) {
  var resultsDiv = document.getElementById('relation-search-results');
  try {
    var res = await authFetch('/api/buckets');
    var data = await res.json();
    var buckets = data.buckets || data;
    
    var filtered = buckets.filter(function(b) {
      if (b.id === sourceId) return false;
      var name = b.name || b.topic || '';
      return name.toLowerCase().includes(query.toLowerCase()) ||
             (b.tags || []).some(function(t) { return t.toLowerCase().includes(query.toLowerCase()); });
    }).slice(0, 20);
    
    if (filtered.length === 0) {
      resultsDiv.innerHTML = '<div style="color:var(--text-dim);text-align:center;padding:20px;">没有找到匹配的记忆桶</div>';
      return;
    }
    
    resultsDiv.innerHTML = filtered.map(function(b) {
      var relType = '';
      return `
        <div style="display:flex;justify-content:space-between;align-items:center;padding:10px;border-radius:8px;margin-bottom:6px;cursor:pointer;"
             onmouseover="this.style.background=var(--border)" onmouseout="this.style.background=''"
             onclick="addRelationToBucket('${sourceId}', '${b.id}')">
          <div>
            <div style="font-size:14px;">${escapeHtml(b.name || b.topic || '未命名')}</div>
            <div style="font-size:11px;color:var(--text-dim);">${b.id}</div>
          </div>
          <button style="padding:6px 12px;border:none;background:var(--accent);color:white;border-radius:8px;cursor:pointer;font-size:12px;">关联</button>
        </div>
      `;
    }).join('');
  } catch (e) {
    resultsDiv.innerHTML = '<div style="color:var(--negative);text-align:center;padding:20px;">加载失败: ' + e.message + '</div>';
  }
}

async function addRelationToBucket(sourceId, targetId) {
  try {
    var resp = await authFetch('/api/manage-relation', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: 'link', bucket_id: sourceId, target_id: targetId})
    });
    var result = await resp.json();
    var msgDiv = document.getElementById('relation-add-msg');
    if (result.success) {
      msgDiv.textContent = '关联成功!';
      msgDiv.style.color = 'var(--accent)';
      showDetail(sourceId);
      setTimeout(function() {
        document.querySelector('.modal-overlay')?.remove();
      }, 1000);
    } else {
      msgDiv.textContent = '关联失败: ' + result.message;
      msgDiv.style.color = 'var(--negative)';
    }
  } catch (e) {
    document.getElementById('relation-add-msg').textContent = '关联失败: ' + e.message;
  }
}

function closeDetail() {
  document.getElementById('detail-panel').classList.remove('open');
}

var networkData = null;
async function loadNetwork() {
  var canvas = document.getElementById('network-canvas');
  var ctx = canvas.getContext('2d');
  canvas.width = canvas.offsetWidth * window.devicePixelRatio;
  canvas.height = canvas.offsetHeight * window.devicePixelRatio;
  ctx.scale(window.devicePixelRatio, window.devicePixelRatio);
  var W = canvas.offsetWidth, H = canvas.offsetHeight;

  ctx.fillStyle = '#FDFCF0';
  ctx.fillRect(0, 0, W, H);
  ctx.fillStyle = '#8A8070';
  ctx.font = '14px Inter, sans-serif';
  ctx.textAlign = 'center';
  ctx.fillText('加载记忆网络…', W/2, H/2);

  try {
    var res = await fetch(BASE + '/api/network');
    networkData = await res.json();
    initNetworkView(canvas, ctx, W, H, networkData);
  } catch(e) {
    ctx.fillText('加载失败: ' + e.message, W/2, H/2 + 24);
  }
}

var networkState = {
  zoom: 1,
  panX: 0,
  panY: 0,
  isDragging: false,
  dragNode: null,
  dragStartX: 0,
  dragStartY: 0,
  isPanning: false,
  panStartX: 0,
  panStartY: 0,
  positions: {},
  hoveredNode: null
};

function forceDirectedLayout(nodes, edges, W, H) {
  var cx = W / 2, cy = H / 2;
  
  var typeOrder = ['identity', 'pattern', 'permanent', 'event', 'experience', 'candlestick', 'feel', 'dynamic'];
  
  var typeNodes = {};
  nodes.forEach(function(n) {
    var t = n.type || 'dynamic';
    if (!typeNodes[t]) typeNodes[t] = [];
    typeNodes[t].push(n);
  });
  
  var maxRadius = Math.min(W, H) * 0.38;
  var ringSpacing = maxRadius / (typeOrder.length + 1);
  
  typeOrder.forEach(function(type, ringIndex) {
    var ringNodes = typeNodes[type];
    if (!ringNodes || ringNodes.length === 0) return;
    
    var ringRadius = (ringIndex + 1) * ringSpacing;
    
    ringNodes.forEach(function(n, i) {
      var angle = (i / ringNodes.length) * Math.PI * 2;
      var jitter = (Math.random() - 0.5) * 15;
      networkState.positions[n.id] = {
        x: cx + Math.cos(angle) * ringRadius + jitter,
        y: cy + Math.sin(angle) * ringRadius + jitter,
        vx: 0,
        vy: 0,
        origX: cx + Math.cos(angle) * ringRadius,
        origY: cy + Math.sin(angle) * ringRadius
      };
    });
  });
}

function applyForces(nodes, edges, W, H) {
  var repulsion = 200;
  var attraction = 0.02;
  var damping = 0.92;
  var springLength = 100;
  var boundaryStrength = 0.05;
  
  for (var i = 0; i < nodes.length; i++) {
    for (var j = i + 1; j < nodes.length; j++) {
      var n1 = nodes[i];
      var n2 = nodes[j];
      var p1 = networkState.positions[n1.id];
      var p2 = networkState.positions[n2.id];
      
      var dx = p2.x - p1.x;
      var dy = p2.y - p1.y;
      var dist = Math.sqrt(dx * dx + dy * dy);
      
      if (dist < 1) dist = 1;
      
      var force = repulsion / (dist * dist);
      var fx = (dx / dist) * force;
      var fy = (dy / dist) * force;
      
      p1.vx -= fx;
      p1.vy -= fy;
      p2.vx += fx;
      p2.vy += fy;
    }
  }
  
  edges.forEach(function(e) {
    var p1 = networkState.positions[e.source];
    var p2 = networkState.positions[e.target];
    
    if (!p1 || !p2) return;
    
    var dx = p2.x - p1.x;
    var dy = p2.y - p1.y;
    var dist = Math.sqrt(dx * dx + dy * dy);
    
    if (dist < 1) dist = 1;
    
    var weight = e.weight || 0.5;
    var force = (dist - springLength) * attraction * weight;
    var fx = (dx / dist) * force;
    var fy = (dy / dist) * force;
    
    p1.vx += fx;
    p1.vy += fy;
    p2.vx -= fx;
    p2.vy -= fy;
  });
  
  nodes.forEach(function(n) {
    var p = networkState.positions[n.id];
    
    if (p.origX !== undefined && p.origY !== undefined) {
      var origDx = p.origX - p.x;
      var origDy = p.origY - p.y;
      p.vx += origDx * 0.008;
      p.vy += origDy * 0.008;
    }
    
    if (p.x < 50) p.vx += (50 - p.x) * boundaryStrength;
    if (p.x > W - 50) p.vx += (W - 50 - p.x) * boundaryStrength;
    if (p.y < 50) p.vy += (50 - p.y) * boundaryStrength;
    if (p.y > H - 50) p.vy += (H - 50 - p.y) * boundaryStrength;
    
    p.x += p.vx;
    p.y += p.vy;
    p.vx *= damping;
    p.vy *= damping;
  });
}

function initNetworkView(canvas, ctx, W, H, data) {
  networkState.zoom = 1;
  networkState.panX = 0;
  networkState.panY = 0;
  networkState.positions = {};
  networkState.isPanning = false;
  networkState.isDragging = false;
  networkState.dragNode = null;
  networkState.hoveredNode = null;
  
  var nodes = data.nodes || [];
  var edges = data.edges || [];
  
  if (!nodes.length) {
    ctx.fillStyle = '#8A8070';
    ctx.fillText('没有记忆桶', W/2, H/2);
    return;
  }

  // Force-directed layout
  forceDirectedLayout(nodes, edges, W, H);
  
  // Run iterations to stabilize
  for (var iter = 0; iter < 80; iter++) {
    applyForces(nodes, edges, W, H);
  }

  drawNetwork(canvas, ctx, W, H, nodes, edges);
  
  canvas.onmousedown = function(e) {
    var rect = canvas.getBoundingClientRect();
    var mx = e.clientX - rect.left;
    var my = e.clientY - rect.top;
    var node = getNodeAtPosition(mx, my);
    if (node) {
      networkState.isDragging = true;
      networkState.dragNode = node;
      networkState.dragStartX = mx;
      networkState.dragStartY = my;
    } else {
      networkState.isPanning = true;
      networkState.panStartX = mx;
      networkState.panStartY = my;
    }
  };
  
  var networkAnimationFrame = null;
  
  function scheduleNetworkRedraw() {
    if (networkAnimationFrame) return;
    networkAnimationFrame = requestAnimationFrame(function() {
      drawNetwork(canvas, ctx, W, H, nodes, edges);
      networkAnimationFrame = null;
    });
  }
  
  canvas.onmousemove = function(e) {
    var rect = canvas.getBoundingClientRect();
    var mx = e.clientX - rect.left;
    var my = e.clientY - rect.top;
    
    if (networkState.isDragging && networkState.dragNode) {
      var dx = mx - networkState.dragStartX;
      var dy = my - networkState.dragStartY;
      var pos = networkState.positions[networkState.dragNode.id];
      pos.x += dx / networkState.zoom;
      pos.y += dy / networkState.zoom;
      networkState.dragStartX = mx;
      networkState.dragStartY = my;
      scheduleNetworkRedraw();
    } else if (networkState.isPanning) {
      var dx = mx - networkState.panStartX;
      var dy = my - networkState.panStartY;
      networkState.panX += dx;
      networkState.panY += dy;
      networkState.panStartX = mx;
      networkState.panStartY = my;
      scheduleNetworkRedraw();
    } else {
      var node = getNodeAtPosition(mx, my);
      if (node !== networkState.hoveredNode) {
        networkState.hoveredNode = node;
        scheduleNetworkRedraw();
      }
    }
  };
  
  canvas.onmouseup = function() {
    networkState.isDragging = false;
    networkState.dragNode = null;
    networkState.isPanning = false;
  };
  
  canvas.onmouseleave = function() {
    networkState.isDragging = false;
    networkState.dragNode = null;
    networkState.isPanning = false;
    networkState.hoveredNode = null;
    scheduleNetworkRedraw();
  };
  
  canvas.onwheel = function(e) {
    e.preventDefault();
    var rect = canvas.getBoundingClientRect();
    var mx = e.clientX - rect.left;
    var my = e.clientY - rect.top;
    var delta = e.deltaY > 0 ? 0.9 : 1.1;
    var newZoom = Math.max(0.3, Math.min(3, networkState.zoom * delta));
    
    // Zoom towards mouse position
    var oldZoom = networkState.zoom;
    networkState.zoom = newZoom;
    var cx = W / 2;
    var cy = H / 2;
    networkState.panX = mx - (mx - networkState.panX) * (newZoom / oldZoom);
    networkState.panY = my - (my - networkState.panY) * (newZoom / oldZoom);
    
    scheduleNetworkRedraw();
  };
  
  canvas.onclick = function(e) {
    var rect = canvas.getBoundingClientRect();
    var mx = e.clientX - rect.left;
    var my = e.clientY - rect.top;
    var node = getNodeAtPosition(mx, my);
    if (node) {
      showDetail(node.id);
    }
  };
}

var NETWORK_CONFIG = {
  bgColor: '#FDFCF0',
  typeColors: {
    identity: '#4A7C59',
    pattern: '#6A6A8B',
    event: '#2F4F4F',
    permanent: '#9A7B4F',
    feel: '#8B6A6A',
    experience: '#7B68EE',
    candlestick: '#DAA520',
    dynamic: '#20B2AA',
    archived: '#B0A590',
  },
  typeLabels: {
    identity: '身份',
    pattern: '模式',
    event: '事件',
    permanent: '永久',
    feel: '感受',
    experience: '年轮',
    candlestick: '烛台',
    dynamic: '动态',
    archived: '归档',
  },
  edgeColors: {
    same_event: '#2196F3',
    related: '#4CAF50',
    hierarchy: '#FF9800',
    similarity: '#9C27B0',
    cooccurrence: '#E91E63',
  },
  edgeLabels: {
    same_event: '同一事件',
    related: '相关',
    hierarchy: '层级',
    similarity: '相似',
    cooccurrence: '共享标签',
  },
  edgeDashed: {
    same_event: false,
    related: false,
    hierarchy: true,
    similarity: false,
    cooccurrence: true,
  },
  typeOrder: ['identity', 'pattern', 'permanent', 'event', 'feel', 'experience', 'candlestick', 'dynamic'],
};

function drawNetwork(canvas, ctx, W, H, nodes, edges) {
  var cfg = NETWORK_CONFIG;
  
  ctx.fillStyle = cfg.bgColor;
  ctx.fillRect(0, 0, W, H);
  
  ctx.save();
  ctx.translate(networkState.panX, networkState.panY);
  ctx.scale(networkState.zoom, networkState.zoom);

  drawEdges(ctx, edges, cfg);
  
  drawNodes(ctx, nodes, cfg);
  
  ctx.restore();
  
  drawLegend(ctx, W, cfg);
  
  ctx.font = '11px Inter, sans-serif';
  ctx.fillStyle = '#8A8070';
  ctx.textAlign = 'right';
  ctx.fillText(Math.round(networkState.zoom * 100) + '%', W - 16, 20);
}

function drawEdges(ctx, edges, cfg) {
  var zoom = networkState.zoom;
  
  var visibleEdges = edges.filter(function(e) {
    if (zoom < 0.5) {
      return e.type !== 'cooccurrence';
    }
    if (zoom < 0.8) {
      return e.type !== 'cooccurrence' || (e.weight && e.weight > 0.5);
    }
    return true;
  });
  
  visibleEdges.forEach(function(e) {
    var a = networkState.positions[e.source];
    var b = networkState.positions[e.target];
    if (!a || !b) return;
    
    var color = cfg.edgeColors[e.type] || '#9E9E9E';
    var weight = e.weight || 0.3;
    
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    
    if (cfg.edgeDashed[e.type]) {
      ctx.setLineDash([4, 3]);
    } else {
      ctx.setLineDash([]);
    }
    
    var alpha = zoom < 0.5 ? '30' : (zoom < 0.8 ? '40' : '60');
    ctx.lineTo(b.x, b.y);
    ctx.strokeStyle = color + alpha;
    ctx.lineWidth = Math.max(1, weight * 1.5);
    ctx.stroke();
    ctx.setLineDash([]);
  });
}

function drawNodes(ctx, nodes, cfg) {
  nodes.forEach(function(n) {
    var p = networkState.positions[n.id];
    if (!p) return;
    
    var baseR = n.pinned ? 10 : Math.max(5, Math.min(14, (n.score || 0.5) * 12));
    var r = networkState.zoom > 1.2 ? baseR * 1.1 : baseR;
    var color = cfg.typeColors[n.type] || '#2F4F4F';
    var isHovered = networkState.hoveredNode && networkState.hoveredNode.id === n.id;
    
    var opacity = 'FF';
    var decayStage = n.decay_stage || 1;
    if (decayStage === 2) {
      opacity = 'AA';
    } else if (decayStage === 3) {
      opacity = '66';
    }
    
    if (isHovered) {
      ctx.beginPath();
      ctx.arc(p.x, p.y, r + 8, 0, Math.PI * 2);
      ctx.fillStyle = color + '30';
      ctx.fill();
    }
    
    ctx.beginPath();
    ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
    var fillColor = n.resolved ? color + '60' : color + opacity;
    ctx.fillStyle = fillColor;
    ctx.fill();
    
    if (n.pinned) {
      ctx.beginPath();
      ctx.arc(p.x, p.y, r + 2, 0, Math.PI * 2);
      ctx.strokeStyle = '#FFD700';
      ctx.lineWidth = 2;
      ctx.stroke();
    }
    
    if (n.digested) {
      ctx.beginPath();
      ctx.moveTo(p.x - r * 0.6, p.y - r * 0.6);
      ctx.lineTo(p.x + r * 0.6, p.y + r * 0.6);
      ctx.moveTo(p.x + r * 0.6, p.y - r * 0.6);
      ctx.lineTo(p.x - r * 0.6, p.y + r * 0.6);
      ctx.strokeStyle = '#FFF';
      ctx.lineWidth = 2;
      ctx.stroke();
    }
    
    if (isHovered || networkState.zoom > 1.5 || r > 12) {
      var name = n.name.length > 10 ? n.name.slice(0, 10) + '…' : n.name;
      ctx.fillStyle = '#3A3530';
      ctx.font = 'bold 11px Inter, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText(name, p.x, p.y + r + 16);
    }
  });
}

function drawLegend(ctx, W, cfg) {
  var legendY = 16;
  var legendItemHeight = 24;
  
  ctx.fillStyle = '#3A3530';
  ctx.font = 'bold 12px Inter, sans-serif';
  ctx.textAlign = 'left';
  ctx.fillText('记忆类型', 16, legendY);
  
  legendY += legendItemHeight;
  
  cfg.typeOrder.forEach(function(type) {
    var color = cfg.typeColors[type];
    var label = cfg.typeLabels[type];
    if (color && label) {
      ctx.beginPath();
      ctx.arc(20, legendY - 8, 6, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();
      
      ctx.fillStyle = '#5A5550';
      ctx.font = '11px Inter, sans-serif';
      ctx.fillText(label, 32, legendY - 4);
      legendY += legendItemHeight;
    }
  });
  
  legendY += 8;
  ctx.fillStyle = '#3A3530';
  ctx.font = 'bold 12px Inter, sans-serif';
  ctx.fillText('连接类型', 16, legendY);
  legendY += legendItemHeight;
  
  for (var etype in cfg.edgeLabels) {
    ctx.fillStyle = cfg.edgeColors[etype] || '#9E9E9E';
    ctx.font = '11px Inter, sans-serif';
    ctx.fillText(cfg.edgeLabels[etype], 32, legendY - 4);
    legendY += legendItemHeight;
  }
}

function getNodeAtPosition(mx, my) {
  var positions = networkState.positions;
  var nodes = networkData ? networkData.nodes : [];
  
  // Convert mouse position to world coordinates
  var worldX = (mx - networkState.panX) / networkState.zoom;
  var worldY = (my - networkState.panY) / networkState.zoom;
  
  for (var i = nodes.length - 1; i >= 0; i--) {
    var n = nodes[i];
    var p = positions[n.id];
    if (!p) continue;
    var r = Math.max(6, Math.min(16, n.score * 12)) + 4;
    var dx = worldX - p.x, dy = worldY - p.y;
    if (dx*dx + dy*dy < r*r) return n;
  }
  return null;
}

function networkZoomIn() {
  networkState.zoom = Math.min(3, networkState.zoom * 1.2);
  redrawNetwork();
}

function networkZoomOut() {
  networkState.zoom = Math.max(0.3, networkState.zoom / 1.2);
  redrawNetwork();
}

function networkReset() {
  networkState.zoom = 1;
  networkState.panX = 0;
  networkState.panY = 0;
  // Reset positions to initial circular layout
  if (networkData) {
    var canvasEl = document.getElementById('network-canvas');
    var W = canvasEl.offsetWidth, H = canvasEl.offsetHeight;
    var cx = W / 2, cy = H / 2;
    var r = Math.min(W, H) * 0.35;
    networkData.nodes.forEach(function(n, i) {
      var angle = (i / networkData.nodes.length) * Math.PI * 2;
      networkState.positions[n.id] = {
        x: cx + Math.cos(angle) * r,
        y: cy + Math.sin(angle) * r
      };
    });
  }
  redrawNetwork();
}

function redrawNetwork() {
  var canvas = document.getElementById('network-canvas');
  var ctx = canvas.getContext('2d');
  var W = canvas.offsetWidth, H = canvas.offsetHeight;
  if (networkData) {
    drawNetwork(canvas, ctx, W, H, networkData.nodes, networkData.edges);
  }
}

function esc(s) {
  if (!s) return '';
  var d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function escapeHtml(s) {
  return esc(s);
}

function formatTimeAgo(iso) {
  if (!iso) return '—';
  var d = new Date(iso);
  var now = new Date();
  var hours = Math.floor((now - d) / 3600000);
  if (hours < 1) return '刚刚';
  if (hours < 24) return hours + 'h前';
  var days = Math.floor(hours / 24);
  if (days < 30) return days + 'd前';
  return Math.floor(days/30) + 'mo前';
}

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') closeDetail();
  if (e.key === '/' && !e.ctrlKey && !e.metaKey && document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'TEXTAREA') {
    e.preventDefault();
    toggleAIChat();
  }
});

async function checkAIStatus() {
  var badge = document.getElementById('ai-status-badge');
  try {
    var res = await fetch(BASE + '/api/config');
    var cfg = await res.json();
    var hasKey = cfg.dehydration && cfg.dehydration.api_key_masked && cfg.dehydration.api_key_masked !== '';
    if (hasKey) {
      badge.innerHTML = 'AI: ✓ 已接入';
      badge.style.background = 'rgba(74,124,89,0.15)';
      badge.style.color = '#4A7C59';
      badge.style.borderColor = 'rgba(74,124,89,0.3)';
    } else {
      badge.innerHTML = 'AI: ✗ 未配置';
      badge.style.background = 'rgba(139,74,74,0.15)';
      badge.style.color = '#8B4A4A';
      badge.style.borderColor = 'rgba(139,74,74,0.3)';
    }
  } catch (e) {
    badge.innerHTML = 'AI: 未知';
    badge.style.background = 'rgba(160,140,110,0.1)';
    badge.style.color = 'var(--text-dim)';
    badge.style.borderColor = 'var(--border)';
  }
}

async function loadConfig() {
  try {
    var res = await fetch(BASE + '/api/config');
    var cfg = await res.json();
    document.getElementById('cfg-dehy-model').value = cfg.dehydration.model || '';
    document.getElementById('cfg-dehy-url').value = cfg.dehydration.base_url || '';
    var dehyKeyMasked = cfg.dehydration.api_key_masked || '';
    document.getElementById('cfg-dehy-key').placeholder = '当前: ' + (dehyKeyMasked || '未设置');
    document.getElementById('cfg-dehy-key').value = dehyKeyMasked ? '******' : '';
  } catch (e) {
    document.getElementById('config-status').innerHTML =
      '<span style="color:var(--negative)">加载失败: ' + e.message + '</span>';
  }
}

async function saveConfig(persist) {
  var body = {
    dehydration: {
      model: document.getElementById('cfg-dehy-model').value,
      base_url: document.getElementById('cfg-dehy-url').value,
    },
    persist: persist,
  };
  var dehyKeyVal = document.getElementById('cfg-dehy-key').value;
  if (dehyKeyVal && dehyKeyVal !== '******') body.dehydration.api_key = dehyKeyVal;

  var status = document.getElementById('config-status');
  try {
    var res = await fetch(BASE + '/api/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    var result = await res.json();
    if (result.ok) {
      status.innerHTML = '<span style="color:var(--positive)">✓ 已更新: ' + result.updated.join(', ') + '</span>';
      loadConfig();
    } else {
      status.innerHTML = '<span style="color:var(--negative)">✗ ' + (result.error || '未知错误') + '</span>';
    }
  } catch (e) {
    status.innerHTML = '<span style="color:var(--negative)">✗ 请求失败: ' + e.message + '</span>';
  }
}

async function testAIConnection() {
  var btn = document.getElementById('btn-ai-test');
  var status = document.getElementById('ai-status');
  btn.disabled = true;
  btn.style.opacity = '0.6';
  status.innerHTML = '<span style="color:var(--warning)">测试中...</span>';
  
  try {
    var res = await fetch(BASE + '/api/ai-test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
    });
    var result = await res.json();
    if (result.ok) {
      status.innerHTML = '<span style="color:var(--positive)">✓ AI连接正常</span>';
    } else {
      status.innerHTML = '<span style="color:var(--negative)">✗ ' + (result.error || '连接失败') + '</span>';
    }
  } catch (e) {
    status.innerHTML = '<span style="color:var(--negative)">✗ 请求失败: ' + e.message + '</span>';
  } finally {
    btn.disabled = false;
    btn.style.opacity = '1';
  }
}

// ========================================
// Brain Export/Import / 大脑导出导入
// ========================================
async function doExportBrain(outputPath) {
  var status = document.getElementById('export-status');
  status.innerHTML = '<span style="color:var(--warning)">导出中...</span>';
  
  try {
    var body = {};
    if (outputPath && outputPath.trim()) {
      body.output_path = outputPath.trim();
    }
    
    var res = await fetch(BASE + '/api/export-brain', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    var result = await res.json();
    
    if (result.ok) {
      status.innerHTML = '<span style="color:var(--positive)">✓ 导出成功！</span><br/>' +
        '<span style="font-size:12px;color:var(--text-dim)">文件路径: ' + result.path + '</span><br/>' +
        '<span style="font-size:12px;color:var(--text-dim)">大小: ' + result.size + '</span>';
    } else {
      status.innerHTML = '<span style="color:var(--negative)">✗ 导出失败: ' + (result.error || '未知错误') + '</span>';
    }
  } catch (e) {
    status.innerHTML = '<span style="color:var(--negative)">✗ 请求失败: ' + e.message + '</span>';
  }
}

async function doImportBrain() {
  var status = document.getElementById('import-status');
  var zipPath = document.getElementById('import-path').value;
  var overwrite = document.getElementById('import-overwrite').checked;
  
  if (!zipPath || !zipPath.trim()) {
    status.innerHTML = '<span style="color:var(--warning)">请输入 zip 文件路径</span>';
    return;
  }
  
  status.innerHTML = '<span style="color:var(--warning)">导入中...</span>';
  
  try {
    var res = await fetch(BASE + '/api/import-brain', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        zip_path: zipPath.trim(),
        overwrite: overwrite,
      }),
    });
    var result = await res.json();
    
    if (result.ok) {
      status.innerHTML = '<span style="color:var(--positive)">✓ 导入成功！</span><br/>' +
        '<span style="font-size:12px;color:var(--text-dim)">' + result.message + '</span>';
    } else {
      status.innerHTML = '<span style="color:var(--negative)">✗ 导入失败: ' + (result.error || '未知错误') + '</span>';
    }
  } catch (e) {
    status.innerHTML = '<span style="color:var(--negative)">✗ 请求失败: ' + e.message + '</span>';
  }
}



checkAuth().then((authenticated) => {
  if (authenticated) {
    loadBuckets();
    checkAIStatus();
  }
});

var selectedMemories = new Set();

function toggleSelect(id) {
  if (selectedMemories.has(id)) {
    selectedMemories.delete(id);
  } else {
    selectedMemories.add(id);
  }
  updateBatchDeleteButton();
}

function selectAll() {
  var checkboxes = document.querySelectorAll('.memory-checkbox');
  checkboxes.forEach(function(cb) {
    cb.checked = true;
    var id = cb.getAttribute('data-id');
    selectedMemories.add(id);
  });
  updateBatchDeleteButton();
}

function deselectAll() {
  var checkboxes = document.querySelectorAll('.memory-checkbox');
  checkboxes.forEach(function(cb) {
    cb.checked = false;
  });
  selectedMemories.clear();
  updateBatchDeleteButton();
}

function updateBatchDeleteButton() {
  var btn = document.getElementById('batch-delete-btn');
  if (btn) {
    if (selectedMemories.size > 0) {
      btn.style.display = 'flex';
      btn.innerHTML = `🗑️ 删除选中 (${selectedMemories.size})`;
    } else {
      btn.style.display = 'none';
    }
  }
}

async function batchDelete() {
  if (selectedMemories.size === 0) return;
  
  if (!confirm(`确定要删除选中的 ${selectedMemories.size} 条记忆吗？`)) {
    return;
  }
  
  var btn = document.getElementById('batch-delete-btn');
  if (btn) btn.innerHTML = '删除中...';
  
  var deleted = 0;
  var errors = 0;
  
  for (var id of selectedMemories) {
    try {
      await fetch(BASE + '/api/bucket/' + id, { method: 'DELETE' });
      deleted++;
    } catch {
      errors++;
    }
  }
  
  selectedMemories.clear();
  updateBatchDeleteButton();
  
  if (btn) btn.innerHTML = '删除完成';
  setTimeout(function() {
    loadDirectory();
  }, 500);
}

function renderDirectory(data) {
  const stats = document.getElementById('directory-stats');
  const content = document.getElementById('directory-content');
  const empty = document.getElementById('directory-empty');
  
  if (!stats || !content) {
    console.error('directory-stats or directory-content element not found');
    return;
  }
  
  stats.innerHTML = '';
  content.innerHTML = '';
  
  if (!data || !data.sections || !Array.isArray(data.sections)) {
    content.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-dim);">数据格式错误</div>';
    return;
  }
  
  if (data.total === 0 || data.sections.length === 0) {
    if (empty) empty.style.display = 'block';
    return;
  }
  if (empty) empty.style.display = 'none';
  
  const typeColors = {
    identity: '#4A7C59',
    pattern: '#9A7B4F',
    event: '#2F4F4F',
    permanent: '#6A6A8B',
    feel: '#8B6A6A',
    experience: '#7B68EE',
    candlestick: '#DAA520',
    dynamic: '#20B2AA',
  };
  
  const statCards = [
    { label: '总记忆', value: data.total, color: '#6A6A8B' },
    { label: '身份档案', value: data.sections.find(s => s.type === 'identity')?.count || 0, color: typeColors.identity },
    { label: '行为模式', value: data.sections.find(s => s.type === 'pattern')?.count || 0, color: typeColors.pattern },
    { label: '事件记忆', value: data.sections.find(s => s.type === 'event')?.count || 0, color: typeColors.event },
    { label: '年轮', value: data.sections.find(s => s.type === 'experience')?.count || 0, color: typeColors.experience },
    { label: '烛台', value: data.sections.find(s => s.type === 'candlestick')?.count || 0, color: typeColors.candlestick },
    { label: '动态记忆', value: data.sections.find(s => s.type === 'dynamic')?.count || 0, color: typeColors.dynamic },
  ];
  
  stats.innerHTML = statCards.map(s => `
    <div style="background:var(--surface);border-radius:10px;padding:14px 12px;border:1px solid var(--border);text-align:center;">
      <div style="font-size:22px;font-weight:bold;color:${s.color};">${s.value}</div>
      <div style="font-size:11px;color:var(--text-dim);margin-top:3px;">${s.label}</div>
    </div>
  `).join('');
  
  content.innerHTML = `
    <div style="display:flex;justify-content:flex-end;align-items:center;gap:8px;margin-bottom:16px;padding:12px;background:var(--surface);border-radius:12px;border:1px solid var(--border);">
      <button onclick="selectAll()" style="padding:6px 12px;border:none;border-radius:8px;background:var(--text);color:white;font-size:12px;cursor:pointer;transition:background 0.2s;">全选</button>
      <button onclick="deselectAll()" style="padding:6px 12px;border:none;border-radius:8px;background:var(--border);color:var(--text);font-size:12px;cursor:pointer;transition:background 0.2s;">取消</button>
      <button id="batch-delete-btn" onclick="batchDelete()" style="padding:6px 12px;border:none;border-radius:8px;background:#8B4A4A;color:white;font-size:12px;cursor:pointer;transition:background 0.2s;display:none;">🗑️ 删除选中</button>
    </div>
  `;
  
  const typeOrder = ['identity', 'pattern', 'permanent', 'event', 'experience', 'candlestick', 'feel', 'dynamic'];
  const sortedSections = [...data.sections].sort((a, b) => {
    const idxA = typeOrder.indexOf(a.type);
    const idxB = typeOrder.indexOf(b.type);
    return (idxA === -1 ? 100 : idxA) - (idxB === -1 ? 100 : idxB);
  });
  
  sortedSections.forEach(section => {
    const entries = section.entries || [];
    const sectionColor = typeColors[section.type] || '#6A6A8B';
    
    const entriesHtml = entries.map(entry => {
      const decayLabels = ['完整', '总结', '已消化'];
      const decayColors = ['#4A7C59', '#9A7B4F', '#8B6A6A'];
      const decayStage = entry.decay_stage || 1;
      const isPinned = entry.pinned || entry.protected;
      const score = entry.score ? parseFloat(entry.score).toFixed(1) : '';
      const safeId = JSON.stringify(entry.id);
      const safeName = entry.name ? entry.name.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;') : entry.id;
      const emotions = entry.emotions || [];
      const tags = entry.tags || [];
      const importance = entry.importance || 5;
      const importanceColor = getImportanceColor(importance);
      const importanceWidth = (importance / 10) * 100;
      
      return `
        <div class="memory-item${isPinned ? ' pinned' : ''}" 
             style="background:var(--surface);border-radius:12px;padding:14px;border:1px solid var(--border);cursor:pointer;transition:all 0.2s;box-shadow:0 1px 3px rgba(0,0,0,0.04);">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;">
            <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;flex:1;">
              <input type="checkbox" class="memory-checkbox" data-id=${safeId} 
                     onclick="event.stopPropagation();toggleSelect(${safeId})"
                     style="width:16px;height:16px;border:2px solid var(--border);border-radius:4px;cursor:pointer;accent-color:${sectionColor};">
              ${isPinned ? '<span style="font-size:14px;">📌</span>' : ''}
              <span style="font-weight:600;font-size:13px;color:var(--text);line-height:1.4;" onclick="showDetail(${safeId})">${safeName}</span>
            </div>
            <div style="display:flex;align-items:center;gap:6px;">
              ${score ? `<span style="font-size:11px;color:${sectionColor};font-weight:600;background:${sectionColor}10;padding:2px 6px;border-radius:6px;">${score}</span>` : ''}
              ${entry.importance > 0 ? `<span style="font-size:11px;color:${importanceColor};font-weight:600;">★${importance}</span>` : ''}
            </div>
          </div>
          <div style="display:flex;align-items:center;gap:10px;font-size:10px;color:var(--text-dim);margin-bottom:8px;">
            <span style="display:flex;align-items:center;gap:3px;">
              <span style="width:6px;height:6px;border-radius:50%;background:${decayColors[decayStage-1]};"></span>
              <span style="color:${decayColors[decayStage-1]};font-weight:500;">${decayLabels[decayStage-1]}</span>
            </span>
            ${entry.activation_count > 0 ? `<span style="display:flex;align-items:center;gap:2px;">🔄 ${entry.activation_count}次</span>` : ''}
            ${entry.created ? `<span>📅 ${entry.created.slice(0,10)}</span>` : ''}
          </div>
          <div style="height:2px;background:var(--border);border-radius:1px;overflow:hidden;margin-bottom:8px;">
            <div style="height:100%;width:${importanceWidth}%;background:linear-gradient(90deg, ${importanceColor}, ${importanceColor}80);border-radius:1px;transition:width 0.5s;"></div>
          </div>
          <div style="display:flex;flex-wrap:wrap;gap:4px;">
            ${emotions.map(e => `<span style="background:#FFB74D15;color:#FFB74D;padding:2px 7px;border-radius:10px;font-size:9px;font-weight:500;">${e.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')}</span>`).join('')}
            ${tags.map(t => `<span style="background:${sectionColor}15;color:${sectionColor};padding:2px 7px;border-radius:10px;font-size:9px;font-weight:500;">#${t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')}</span>`).join('')}
          </div>
        </div>
      `;
    }).join('');
    
    const sectionHtml = `
      <div style="margin-bottom:20px;">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px;">
          <div>
            <h3 style="margin:0;font-size:15px;color:var(--text);display:flex;align-items:center;gap:8px;">
              <span style="width:8px;height:8px;border-radius:50%;background:${sectionColor};"></span>
              ${section.label}
            </h3>
            ${section.summary_name ? `<div style="font-size:12px;color:${sectionColor};margin-top:2px;">${section.summary_name}</div>` : ''}
            ${section.summary_desc ? `<div style="font-size:11px;color:var(--text-dim);margin-top:1px;">${section.summary_desc}</div>` : ''}
          </div>
          <span style="font-size:12px;color:var(--text-dim);background:var(--surface);padding:4px 10px;border-radius:12px;">${section.count} 条</span>
        </div>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px;">
          ${entriesHtml}
        </div>
      </div>
    `;
    content.innerHTML += sectionHtml;
  });
  
  if (data.emotions && data.emotions.length > 0) {
    const maxEmotionCount = Math.max(...data.emotions.map(e => e[1]));
    content.innerHTML += `
      <div style="background:var(--surface);border-radius:12px;padding:16px;border:1px solid var(--border);margin-bottom:16px;">
        <h3 style="margin:0 0 12px 0;font-size:14px;color:var(--text-dim);font-weight:500;">情绪分布</h3>
        <div style="display:flex;flex-direction:column;gap:8px;">
          ${data.emotions.map(([emo, cnt]) => {
            const widthPercent = (cnt / maxEmotionCount) * 100;
            return `
              <div style="display:flex;align-items:center;gap:10px;">
                <span style="width:40px;font-size:12px;color:var(--text);">${emo}</span>
                <div style="flex:1;height:8px;background:var(--border);border-radius:4px;overflow:hidden;">
                  <div style="height:100%;width:${widthPercent}%;background:#FFB74D;border-radius:4px;transition:width 0.3s;"></div>
                </div>
                <span style="width:30px;font-size:11px;color:var(--text-dim);text-align:right;">${cnt}</span>
              </div>
            `;
          }).join('')}
        </div>
      </div>
    `;
  }
  
  if (data.tags && data.tags.length > 0) {
    const maxTagCount = Math.max(...data.tags.map(t => t[1]));
    content.innerHTML += `
      <div style="background:var(--surface);border-radius:12px;padding:16px;border:1px solid var(--border);">
        <h3 style="margin:0 0 12px 0;font-size:14px;color:var(--text-dim);font-weight:500;">热门标签</h3>
        <div style="display:flex;flex-wrap:wrap;gap:8px;">
          ${data.tags.map(([tag, cnt]) => {
            const fontSize = 11 + (cnt / maxTagCount) * 4;
            const opacity = 0.6 + (cnt / maxTagCount) * 0.4;
            return `
              <span style="background:#6A6A8B15;color:#6A6A8B;padding:4px 10px;border-radius:16px;font-size:${fontSize}px;opacity:${opacity};transition:opacity 0.2s;" onmouseover="this.style.opacity=1;" onmouseout="this.style.opacity=${opacity};">#${tag} <span style="font-size:10px;opacity:0.7;">${cnt}</span></span>
            `;
          }).join('')}
        </div>
      </div>
    `;
  }

  if (data.analytics) {
    try {
      renderAnalyticsInDirectory(data.analytics, content);
    } catch(e) {
      console.warn('Failed to render analytics:', e);
    }
  }
  
  try {
    renderDirectoryCharts(data);
  } catch(e) {
    console.warn('Failed to render charts:', e);
  }
}

function getImportanceColor(importance) {
  var imp = Math.min(10, Math.max(0, importance));
  var colors = [
    '#C8C8CC',
    '#A8A8B8',
    '#8888A8',
    '#6888C8',
    '#48A8E8',
    '#47798C',
    '#4A7C59',
    '#9A7B4F',
    '#FFB74D',
    '#FF9800'
  ];
  return colors[Math.floor(imp) - 1] || '#6A6A8B';
}

function renderDirectoryCharts(data) {
  var typeCounts = {};
  data.sections.forEach(function(s) {
    typeCounts[s.type] = s.count;
  });
  
  drawTypeChart(typeCounts);
  drawImportanceChart(data);
  drawImportanceRadar(data);
  drawHeatmap(data);
}

function drawTypeChart(counts) {
  var canvas = document.getElementById('directory-type-chart');
  if (!canvas) return;
  
  if (window.typeChartInstance) {
    window.typeChartInstance.destroy();
  }
  
  var types = ['identity', 'pattern', 'event', 'permanent', 'feel', 'experience', 'candlestick', 'dynamic'];
  var labels = ['身份档案', '行为模式', '事件记忆', '永久记忆', '感受记忆', '年轮', '烛台', '动态记忆'];
  var colors = [
    '#4A7C59', '#9A7B4F', '#2F4F4F', '#6A6A8B', 
    '#8B6A6A', '#7B68EE', '#DAA520', '#20B2AA'
  ];
  
  var dataValues = types.map(function(t) { return counts[t] || 0; });
  var total = dataValues.reduce(function(a, b) { return a + b; }, 0);
  
  var ctx = canvas.getContext('2d');
  
  var data = {
    labels: labels,
    datasets: [{
      data: dataValues,
      backgroundColor: colors,
      borderColor: '#FDFCF0',
      borderWidth: 3,
      hoverOffset: 10
    }]
  };
  
  var options = {
    responsive: true,
    maintainAspectRatio: false,
    cutout: '60%',
    animation: {
      animateRotate: true,
      animateScale: true,
      duration: 1200,
      easing: 'easeOutQuart'
    },
    plugins: {
      legend: {
        position: 'right',
        labels: {
          padding: 12,
          usePointStyle: true,
          pointStyle: 'circle',
          font: {
            family: "'Inter', sans-serif",
            size: 12,
            weight: 500
          },
          color: '#5A5550'
        }
      },
      tooltip: {
        backgroundColor: 'rgba(253, 252, 240, 0.95)',
        titleColor: '#3A3530',
        bodyColor: '#5A5550',
        borderColor: 'rgba(180, 165, 140, 0.3)',
        borderWidth: 1,
        padding: 12,
        displayColors: true,
        callbacks: {
          label: function(context) {
            var value = context.raw;
            var percentage = total > 0 ? ((value / total) * 100).toFixed(1) : 0;
            return context.label + ': ' + value + ' (' + percentage + '%)';
          }
        }
      }
    }
  };
  
  if (total === 0) {
    options.plugins.legend.display = false;
    options.plugins.tooltip.enabled = false;
  }
  
  window.typeChartInstance = new Chart(ctx, {
    type: 'doughnut',
    data: data,
    options: options
  });
}

function drawImportanceChart(data) {
  var canvas = document.getElementById('directory-importance-chart');
  if (!canvas) return;
  
  if (window.importanceChartInstance) {
    window.importanceChartInstance.destroy();
  }
  
  var impRanges = [
    { min: 0, max: 1.5, label: '0-1.5', desc: '极低' },
    { min: 1.5, max: 2.5, label: '1.5-2.5', desc: '很低' },
    { min: 2.5, max: 3.5, label: '2.5-3.5', desc: '较低' },
    { min: 3.5, max: 4.5, label: '3.5-4.5', desc: '中低' },
    { min: 4.5, max: 5.5, label: '4.5-5.5', desc: '中等' },
    { min: 5.5, max: 6.5, label: '5.5-6.5', desc: '中高' },
    { min: 6.5, max: 7.5, label: '6.5-7.5', desc: '较高' },
    { min: 7.5, max: 8.5, label: '7.5-8.5', desc: '很高' },
    { min: 8.5, max: 9.5, label: '8.5-9.5', desc: '极高' },
    { min: 9.5, max: 10.5, label: '9.5-10', desc: '最高' }
  ];
  
  var impCounts = [0,0,0,0,0,0,0,0,0,0];
  var total = 0;
  data.sections.forEach(function(s) {
    s.entries.forEach(function(e) {
      var imp = Math.min(10, Math.max(0, e.importance || 5));
      for (var i = 0; i < impRanges.length; i++) {
        if (imp >= impRanges[i].min && imp < impRanges[i].max) {
          impCounts[i]++;
          break;
        }
      }
      total++;
    });
  });
  
  var labels = impRanges.map(function(r) { return r.label; });
  
  var colorStops = [
    { r: 220, g: 220, b: 225 },
    { r: 200, g: 205, b: 215 },
    { r: 170, g: 180, b: 195 },
    { r: 130, g: 150, b: 175 },
    { r: 80, g: 110, b: 150 },
    { r: 47, g: 79, b: 79 },
    { r: 74, g: 124, b: 89 },
    { r: 154, g: 123, b: 79 },
    { r: 255, g: 183, b: 77 },
    { r: 255, g: 100, b: 0 }
  ];
  
  var bgColors = colorStops.map(function(c) {
    return 'rgba(' + c.r + ',' + c.g + ',' + c.b + ', 0.85)';
  });
  
  var borderColors = colorStops.map(function(c) {
    return 'rgba(' + (c.r - 20) + ',' + (c.g - 20) + ',' + (c.b - 20) + ', 1)';
  });
  
  var ctx = canvas.getContext('2d');
  
  var chartData = {
    labels: labels,
    datasets: [{
      label: '数量',
      data: impCounts,
      backgroundColor: bgColors,
      borderColor: borderColors,
      borderWidth: 2,
      borderRadius: 6,
      borderSkipped: false
    }]
  };
  
  var options = {
    responsive: true,
    maintainAspectRatio: false,
    animation: {
      duration: 1000,
      easing: 'easeOutQuart'
    },
    scales: {
      x: {
        grid: {
          display: false
        },
        ticks: {
          font: {
            family: "'Inter', sans-serif",
            size: 12,
            weight: 600
          },
          color: '#5A5550',
          padding: 8
        }
      },
      y: {
        beginAtZero: true,
        grid: {
          color: 'rgba(180, 165, 140, 0.2)'
        },
        ticks: {
          font: {
            family: "'Inter', sans-serif",
            size: 11
          },
          color: '#8A8070',
          padding: 8,
          stepSize: 1
        }
      }
    },
    plugins: {
      legend: {
        display: false
      },
      tooltip: {
        backgroundColor: 'rgba(253, 252, 240, 0.95)',
        titleColor: '#3A3530',
        bodyColor: '#5A5550',
        borderColor: 'rgba(180, 165, 140, 0.3)',
        borderWidth: 1,
        padding: 12,
        displayColors: true,
        callbacks: {
          title: function(context) {
            var index = context[0].dataIndex;
            var range = impRanges[index];
            return '重要度 ' + range.label + ' (' + range.desc + ')';
          },
          label: function(context) {
            var value = context.raw;
            var percentage = total > 0 ? ((value / total) * 100).toFixed(1) : 0;
            return '数量: ' + value + ' (' + percentage + '%)';
          }
        }
      }
    },
    interaction: {
      intersect: false,
      mode: 'index'
    }
  };
  
  window.importanceChartInstance = new Chart(ctx, {
    type: 'bar',
    data: chartData,
    options: options
  });
}

function drawImportanceRadar(data) {
  var canvas = document.getElementById('directory-importance-radar');
  if (!canvas) return;
  
  if (window.importanceRadarInstance) {
    window.importanceRadarInstance.destroy();
  }
  
  var dimensions = [
    { key: 'impact', label: '影响力' },
    { key: 'duration', label: '持续时间' },
    { key: 'emotional_intensity', label: '情绪强度' },
    { key: 'recurrence', label: '复发频率' },
    { key: 'interconnectedness', label: '关联性' }
  ];
  
  var dimSums = [0, 0, 0, 0, 0];
  var dimCounts = [0, 0, 0, 0, 0];
  
  data.sections.forEach(function(s) {
    s.entries.forEach(function(e) {
      var details = e.importance_details || {};
      dimensions.forEach(function(dim, i) {
        var val = details[dim.key];
        if (val !== undefined && val !== null && val > 0) {
          dimSums[i] += val;
          dimCounts[i]++;
        }
      });
    });
  });
  
  var avgValues = dimSums.map(function(sum, i) {
    return dimCounts[i] > 0 ? (sum / dimCounts[i]).toFixed(1) : 0;
  });
  
  var ctx = canvas.getContext('2d');
  
  var chartData = {
    labels: dimensions.map(function(d) { return d.label; }),
    datasets: [{
      label: '平均重要度维度',
      data: avgValues,
      backgroundColor: 'rgba(47, 79, 79, 0.2)',
      borderColor: '#2F4F4F',
      borderWidth: 2,
      pointBackgroundColor: '#2F4F4F',
      pointBorderColor: '#fff',
      pointHoverBackgroundColor: '#fff',
      pointHoverBorderColor: '#2F4F4F',
      pointRadius: 4,
      pointHoverRadius: 6
    }]
  };
  
  var options = {
    responsive: true,
    maintainAspectRatio: false,
    scales: {
      r: {
        angleLines: {
          color: 'rgba(180, 165, 140, 0.2)'
        },
        grid: {
          color: 'rgba(180, 165, 140, 0.2)'
        },
        pointLabels: {
          font: {
            family: "'Inter', sans-serif",
            size: 12,
            weight: 500
          },
          color: '#5A5550'
        },
        suggestedMin: 0,
        suggestedMax: 10,
        ticks: {
          stepSize: 2,
          font: {
            size: 10
          },
          color: '#8A8070',
          backdropColor: 'transparent'
        }
      }
    },
    plugins: {
      legend: {
        display: false
      },
      tooltip: {
        backgroundColor: 'rgba(253, 252, 240, 0.95)',
        titleColor: '#3A3530',
        bodyColor: '#5A5550',
        borderColor: 'rgba(180, 165, 140, 0.3)',
        borderWidth: 1,
        padding: 12,
        callbacks: {
          title: function(context) {
            var dimIndex = parseInt(context[0].dataIndex);
            return dimensions[dimIndex].label;
          },
          label: function(context) {
            var value = context.raw;
            var dimIndex = parseInt(context.dataIndex);
            var count = dimCounts[dimIndex];
            return '平均值: ' + value + ' (样本数: ' + count + ')';
          }
        }
      }
    }
  };
  
  var hasData = avgValues.some(function(v) { return v > 0; });
  if (!hasData) {
    options.plugins.legend.display = false;
    options.plugins.tooltip.enabled = false;
  }
  
  window.importanceRadarInstance = new Chart(ctx, {
    type: 'radar',
    data: chartData,
    options: options
  });
}

function drawHeatmap(data) {
  var container = document.getElementById('directory-heatmap');
  if (!container) return;
  
  var now = new Date();
  var heatmapData = {};
  
  data.sections.forEach(function(s) {
    s.entries.forEach(function(e) {
      var date = e.created || e.last_active || '';
      if (date) {
        var dateStr = date.slice(0, 10);
        heatmapData[dateStr] = (heatmapData[dateStr] || 0) + 1;
      }
    });
  });
  
  var maxCount = Math.max.apply(null, Object.values(heatmapData));
  if (maxCount === 0) {
    container.innerHTML = '<div style="color:var(--text-dim);text-align:center;padding:20px;">暂无活动数据</div>';
    return;
  }
  
  var months = ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月'];
  var html = '<div style="display:flex;flex-direction:column;">';
  
  for (var m = 11; m >= 0; m--) {
    var d = new Date(now.getFullYear(), now.getMonth() - m, 1);
    var monthName = months[d.getMonth()];
    var daysInMonth = new Date(d.getFullYear(), d.getMonth() + 1, 0).getDate();
    
    html += '<div style="display:flex;align-items:center;gap:12px;margin-bottom:4px;">';
    html += '<span style="width:36px;font-size:11px;color:var(--text-dim);">' + monthName + '</span>';
    
    for (var day = 1; day <= daysInMonth; day++) {
      var dateStr = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(day).padStart(2, '0');
      var count = heatmapData[dateStr] || 0;
      var intensity = count / maxCount;
      var color = intensity === 0 ? '#ECEFF1' : 
                  intensity < 0.25 ? '#C8E6C9' : 
                  intensity < 0.5 ? '#81C784' : 
                  intensity < 0.75 ? '#4CAF50' : '#2E7D32';
      
      html += '<div style="width:14px;height:14px;border-radius:3px;background:' + color + ';opacity:' + (intensity > 0 ? 1 : 0.3) + '" title="' + dateStr + ': ' + count + '条"></div>';
    }
    html += '</div>';
  }
  
  html += '</div>';
  html += '<div style="display:flex;align-items:center;gap:6px;margin-top:12px;font-size:11px;color:var(--text-dim);">';
  html += '<span>少</span>';
  html += '<div style="width:14px;height:14px;border-radius:3px;background:#ECEFF1;"></div>';
  html += '<div style="width:14px;height:14px;border-radius:3px;background:#C8E6C9;"></div>';
  html += '<div style="width:14px;height:14px;border-radius:3px;background:#81C784;"></div>';
  html += '<div style="width:14px;height:14px;border-radius:3px;background:#4CAF50;"></div>';
  html += '<div style="width:14px;height:14px;border-radius:3px;background:#2E7D32;"></div>';
  html += '<span>多</span>';
  html += '</div>';
  
  container.innerHTML = html;
}

async function loadDirectory(force = false) {
  const content = document.getElementById('directory-content');
  if (!content) {
    console.error('directory-content element not found');
    return;
  }
  
  try {
    const cached = getCachedData('directory');
    
    if (!force && cached && cached.sections) {
      renderDirectory(cached);
      return;
    }
    
    content.innerHTML = '<div class="loading">加载中…</div>';
    
    const dirResp = await authFetch('/api/directory');
    if (!dirResp) {
      content.innerHTML = '<div class="loading">未登录或会话已过期</div>';
      return;
    }
    
    const dirData = await dirResp.json();
    
    let analyticsData = null;
    try {
      const analyticsResp = await authFetch('/api/analytics');
      if (analyticsResp) {
        analyticsData = await analyticsResp.json();
      }
    } catch(e) {
      console.warn('Failed to load analytics:', e);
    }
    
    dirData.analytics = analyticsData;
    setCachedData('directory', dirData);
    renderDirectory(dirData);
    
    console.log('loadDirectory: completed successfully');
  } catch(e) {
    console.error('Directory load error:', e);
    console.error('Error stack:', e.stack);
    content.innerHTML = `<div class="loading">加载失败: ${e.message}</div>`;
  }
}

// ========================================
// Anchor / 锚点功能
// ========================================
let currentAnchorFilter = '';

function renderAnchors(anchors) {
  const stats = document.getElementById('anchor-stats');
  const list = document.getElementById('anchor-list');
  const empty = document.getElementById('anchor-empty');
  
  if (!anchors || !anchors.length) {
    stats.innerHTML = '';
    list.innerHTML = '';
    empty.style.display = 'block';
    return;
  }
  
  try {
    let highCount = 0, mediumCount = 0;
    for (let i = 0; i < anchors.length; i++) {
      const a = anchors[i];
      if (a.emotion_intensity >= 0.7) highCount++;
      else if (a.emotion_intensity >= 0.4) mediumCount++;
    }
    
    stats.innerHTML = `
      <div style="background:linear-gradient(135deg,#8B6A6A15,#8B6A6A08);border-radius:14px;padding:18px 24px;border:1px solid #8B6A6A20;display:flex;align-items:center;gap:14px;">
        <div style="width:44px;height:44px;border-radius:12px;background:#8B6A6A;display:flex;align-items:center;justify-content:center;color:#fff;font-size:18px;font-weight:600;">${anchors.length}</div>
        <div>
          <div style="font-size:14px;font-weight:500;">总锚点</div>
          <div style="font-size:11px;color:var(--text-dim);">${highCount} 高情绪 · ${mediumCount} 中情绪</div>
        </div>
      </div>
    `;
    
    empty.style.display = 'none';
    
    let filtered = anchors;
    if (currentAnchorFilter === 'high') filtered = anchors.filter(a => a.emotion_intensity >= 0.7);
    else if (currentAnchorFilter === 'medium') filtered = anchors.filter(a => a.emotion_intensity >= 0.4 && a.emotion_intensity < 0.7);
    
    let html = '';
    for (let i = 0; i < filtered.length; i++) {
      const anchor = filtered[i];
      const intensity = anchor.emotion_intensity;
      let intensityColor;
      if (intensity >= 0.9) intensityColor = '#C62828';
      else if (intensity >= 0.8) intensityColor = '#E53935';
      else if (intensity >= 0.7) intensityColor = '#EF5350';
      else if (intensity >= 0.6) intensityColor = '#FFB74D';
      else if (intensity >= 0.5) intensityColor = '#FFCA28';
      else if (intensity >= 0.4) intensityColor = '#8BC34A';
      else if (intensity >= 0.3) intensityColor = '#66BB6A';
      else intensityColor = '#90A4AE';
      
      const valence = anchor.coordinates?.valence ?? 0.5;
      const arousal = anchor.coordinates?.arousal ?? 0.5;
      
      const emotionTags = anchor.emotion_tags || [];
      let tagsHtml = '';
      if (emotionTags.length > 0) {
        tagsHtml = '<div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:8px;">';
        for (let j = 0; j < emotionTags.length; j++) {
          const tag = emotionTags[j];
          const color = getEmotionColor(tag);
          tagsHtml += `<span style="background:${color}15;color:${color};padding:2px 8px;border-radius:6px;font-size:11px;">${tag}</span>`;
        }
        tagsHtml += '</div>';
      }
      
      html += `
        <div style="background:var(--surface);border-radius:16px;padding:20px;border:1px solid var(--border);margin-bottom:14px;transition:all 0.2s;">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px;">
            <div style="flex:1;">
              <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px;">
                <span style="font-size:17px;font-weight:600;color:var(--text);">${anchor.summary}</span>
                <span style="font-size:11px;color:${intensityColor};background:${intensityColor}15;padding:3px 10px;border-radius:12px;font-weight:500;">强度: ${(intensity * 100).toFixed(0)}%</span>
              </div>
              <div style="font-size:12px;color:var(--text-dim);">${anchor.created ? new Date(anchor.created).toLocaleString() : ''}</div>
              ${tagsHtml}
            </div>
            <div style="display:flex;gap:6px;margin-left:16px;">
              <button class="btn-view-bucket" data-bucket-id="${anchor.bucket_id}" style="padding:6px 14px;border-radius:10px;border:1px solid var(--border);background:var(--surface);cursor:pointer;font-size:12px;color:var(--accent);transition:all 0.2s;">查看记忆</button>
              <button class="btn-delete-anchor" data-anchor-id="${anchor.id}" style="padding:6px 14px;border-radius:10px;border:none;background:#FF6B6B15;color:#FF6B6B;cursor:pointer;font-size:12px;font-weight:500;transition:all 0.2s;">删除</button>
            </div>
          </div>
          <div style="display:flex;gap:16px;font-size:12px;color:var(--text-dim);">
            <div style="display:flex;align-items:center;gap:6px;">
              <span style="width:6px;height:6px;border-radius:50%;background:#6A6A8B;"></span>
              记忆桶: <code style="font-size:11px;">#${anchor.bucket_id.substring(0, 8)}</code>
            </div>
            <div style="display:flex;align-items:center;gap:6px;">
              <span style="width:6px;height:6px;border-radius:50%;background:#4A7C59;"></span>
              效价: ${valence.toFixed(2)}
            </div>
            <div style="display:flex;align-items:center;gap:6px;">
              <span style="width:6px;height:6px;border-radius:50%;background:#9A7B4F;"></span>
              唤醒度: ${arousal.toFixed(2)}
            </div>
          </div>
          <div style="margin-top:12px;background:var(--surface-solid);border-radius:8px;padding:10px;">
            <div style="position:relative;width:100%;height:40px;background:linear-gradient(to top, #E5393515, #4A7C5915);border-radius:4px;">
              <div style="position:absolute;left:0;bottom:0;width:100%;height:1px;background:var(--border);"></div>
              <div style="position:absolute;left:0;bottom:0;width:1px;height:100%;background:var(--border);"></div>
              <div style="position:absolute;width:10px;height:10px;border-radius:50%;background:${intensityColor};border:2px solid white;box-shadow:0 1px 4px rgba(0,0,0,0.2);transform:translate(-50%, -50%);left:${valence * 100}%;bottom:${arousal * 100}%;"></div>
            </div>
            <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--text-light);margin-top:4px;">
              <span>负面</span>
              <span>情绪坐标</span>
              <span>正面</span>
            </div>
          </div>
        </div>
      `;
    }
    list.innerHTML = html;
  } catch (e) {
    console.error('renderAnchors failed:', e);
    list.innerHTML = '<div class="loading">渲染失败</div>';
  }
}

async function loadAnchors() {
  const list = document.getElementById('anchor-list');
  try {
    const cached = getCachedData('anchors');
    if (cached) {
      renderAnchors(cached);
      return;
    }
    
    const resp = await authFetch('/api/anchors');
    if (!resp) return;
    const data = await resp.json();
    const anchors = data.anchors || [];
    setCachedData('anchors', anchors);
    renderAnchors(anchors);
  } catch(e) {
    list.innerHTML = `<div class="loading">加载失败: ${e.message}</div>`;
  }
}

function filterAnchors(filter, btn) {
  currentAnchorFilter = filter;
  document.querySelectorAll('.anchor-filter-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  loadAnchors();
}

function showAnchorDetail(bucketId) {
  showDetail(bucketId);
}

function showAnchorEditor() {
  document.getElementById('anchor-edit-id').value = '';
  document.getElementById('anchor-bucket-id').value = '';
  document.getElementById('anchor-bucket-search').value = '';
  document.getElementById('anchor-bucket-selected').style.display = 'none';
  document.getElementById('anchor-bucket-suggestions').style.display = 'none';
  document.getElementById('anchor-intensity').value = '0.7';
  document.getElementById('anchor-intensity-value').textContent = '0.70';
  document.getElementById('anchor-valence').value = '0.5';
  document.getElementById('anchor-valence-value').textContent = '0.50';
  document.getElementById('anchor-arousal').value = '0.5';
  document.getElementById('anchor-arousal-value').textContent = '0.50';
  document.getElementById('anchor-summary').value = '';
  
  renderEmotionTags();
  updateEmotionDot();
  
  document.getElementById('anchor-modal-title').textContent = '添加锚点';
  document.getElementById('anchor-modal').style.display = 'flex';
}

function renderEmotionTags() {
  const container = document.getElementById('anchor-emotion-tags');
  container.innerHTML = '';
  
  const emotionGroups = [
    { title: '愤怒', colors: ['#EF9A9A', '#EF5350', '#E53935', '#C62828'] },
    { title: '恐惧', colors: ['#F48FB1', '#F06292', '#E91E63', '#C2185B'] },
    { title: '悲伤', colors: ['#CE93D8', '#BA68C8', '#9C27B0', '#7B1FA2'] },
    { title: '厌恶', colors: ['#BCAAA4', '#A1887F', '#8D6E63', '#795548'] },
    { title: '惊讶', colors: ['#80DEEA', '#4DD0E1', '#00BCD4', '#0097A7'] },
    { title: '期待', colors: ['#FFE082', '#FFD54F', '#FFC107', '#FFA000'] },
    { title: '信任', colors: ['#A5D6A7', '#66BB6A', '#4CAF50', '#388E3C'] },
    { title: '喜悦', colors: ['#FFCC80', '#FFB74D', '#FF5722', '#E64A19'] }
  ];
  
  emotionGroups.forEach((group, groupIdx) => {
    const groupDiv = document.createElement('div');
    groupDiv.style.display = 'flex';
    groupDiv.style.flexDirection = 'column';
    groupDiv.style.marginBottom = '12px';
    groupDiv.style.gap = '6px';
    
    const titleSpan = document.createElement('span');
    titleSpan.style.fontSize = '11px';
    titleSpan.style.color = 'var(--text-dim)';
    titleSpan.style.fontWeight = '500';
    titleSpan.style.paddingLeft = '4px';
    titleSpan.textContent = group.title;
    groupDiv.appendChild(titleSpan);
    
    const tagsDiv = document.createElement('div');
    tagsDiv.style.display = 'flex';
    tagsDiv.style.flexWrap = 'wrap';
    tagsDiv.style.gap = '6px';
    
    const intensities = ['轻微', '中等', '强烈', '极端'];
    const labels = {
      '愤怒': ['烦恼', '生气', '愤怒', '暴怒'],
      '恐惧': ['不安', '焦虑', '害怕', '恐惧'],
      '悲伤': ['忧伤', '悲伤', '悲痛', '绝望'],
      '厌恶': ['不悦', '反感', '厌恶', '憎恨'],
      '惊讶': ['好奇', '惊讶', '震惊', '惊愕'],
      '期待': ['期待', '希望', '兴奋', '狂喜'],
      '信任': ['接受', '信任', '热爱', '迷恋'],
      '喜悦': ['满意', '快乐', '喜悦', '幸福']
    };
    
    labels[group.title].forEach((label, idx) => {
      const color = group.colors[idx];
      const span = document.createElement('span');
      span.className = 'emotion-tag-option';
      span.dataset.label = label;
      span.dataset.color = color;
      span.style.background = color + '15';
      span.style.color = color;
      span.style.padding = '4px 10px';
      span.style.borderRadius = '8px';
      span.style.fontSize = '12px';
      span.style.cursor = 'pointer';
      span.style.border = '1px solid transparent';
      span.style.transition = 'all 0.2s';
      span.style.boxShadow = '0 1px 3px rgba(0,0,0,0.08)';
      span.textContent = label;
      span.title = `${group.title} - ${intensities[idx]}`;
      span.onmouseenter = () => { span.style.transform = 'translateY(-1px)'; span.style.boxShadow = '0 2px 6px rgba(0,0,0,0.12)'; };
      span.onmouseleave = () => { span.style.transform = ''; span.style.boxShadow = '0 1px 3px rgba(0,0,0,0.08)'; };
      span.onclick = () => toggleEmotionTag(span);
      tagsDiv.appendChild(span);
    });
    
    groupDiv.appendChild(tagsDiv);
    container.appendChild(groupDiv);
  });
}

function toggleEmotionTag(el) {
  const color = el.dataset.color;
  if (el.style.borderColor === 'transparent' || el.style.borderColor === '') {
    el.style.borderColor = color;
    el.style.background = color + '30';
  } else {
    el.style.borderColor = 'transparent';
    el.style.background = color + '15';
  }
}

function toggleEmotionMode(mode) {
  const tagsPanel = document.getElementById('emotion-mode-tags-panel');
  const sliderPanel = document.getElementById('emotion-mode-slider-panel');
  const tagsBtn = document.getElementById('emotion-mode-tags');
  const sliderBtn = document.getElementById('emotion-mode-slider');
  
  if (mode === 'tags') {
    tagsPanel.style.display = 'block';
    sliderPanel.style.display = 'none';
    tagsBtn.style.background = 'var(--accent)';
    tagsBtn.style.color = 'white';
    tagsBtn.style.border = 'none';
    sliderBtn.style.background = 'var(--surface)';
    sliderBtn.style.color = 'var(--text-dim)';
    sliderBtn.style.border = '1px solid var(--border)';
  } else {
    tagsPanel.style.display = 'none';
    sliderPanel.style.display = 'block';
    tagsBtn.style.background = 'var(--surface)';
    tagsBtn.style.color = 'var(--text-dim)';
    tagsBtn.style.border = '1px solid var(--border)';
    sliderBtn.style.background = 'var(--accent)';
    sliderBtn.style.color = 'white';
    sliderBtn.style.border = 'none';
  }
}

function handleEmotionMapClick(event) {
  const rect = event.currentTarget.getBoundingClientRect();
  const x = event.clientX - rect.left;
  const y = event.clientY - rect.top;
  
  const valence = Math.round((x / rect.width) * 20) / 20;
  const arousal = Math.round(((rect.height - y) / rect.height) * 20) / 20;
  
  document.getElementById('anchor-valence').value = valence;
  document.getElementById('anchor-valence-value').textContent = valence.toFixed(2);
  document.getElementById('anchor-arousal').value = arousal;
  document.getElementById('anchor-arousal-value').textContent = arousal.toFixed(2);
  
  updateEmotionDot();
}

function getSelectedEmotionTags() {
  const tags = [];
  document.querySelectorAll('.emotion-tag-option').forEach(el => {
    if (el.style.borderColor !== 'transparent' && el.style.borderColor !== '') {
      tags.push({
        label: el.dataset.label,
        color: el.dataset.color
      });
    }
  });
  return tags;
}

let bucketSearchTimer = null;
document.getElementById('anchor-bucket-search').addEventListener('input', function(e) {
  const query = e.target.value.trim();
  if (bucketSearchTimer) clearTimeout(bucketSearchTimer);
  
  if (!query) {
    document.getElementById('anchor-bucket-suggestions').style.display = 'none';
    return;
  }
  
  bucketSearchTimer = setTimeout(async () => {
    try {
      const buckets = allBuckets.filter(b => 
        b.name.toLowerCase().includes(query.toLowerCase()) || 
        b.id.toLowerCase().includes(query.toLowerCase())
      ).slice(0, 10);
      
      const suggestions = document.getElementById('anchor-bucket-suggestions');
      if (buckets.length === 0) {
        suggestions.style.display = 'none';
        return;
      }
      
      suggestions.innerHTML = buckets.map(b => `
        <div style="padding:10px 14px;cursor:pointer;border-bottom:1px solid var(--border);transition:background 0.2s;" 
             onclick="selectBucket('${b.id}', '${esc(b.name)}')">
          <div style="font-weight:500;color:var(--text);">${esc(b.name)}</div>
          <div style="font-size:11px;color:var(--text-light);">#${b.id.substring(0, 8)}</div>
        </div>
      `).join('');
      suggestions.style.display = 'block';
    } catch (e) {
      console.error('Bucket search failed:', e);
    }
  }, 200);
});

function selectBucket(bucketId, bucketName) {
  document.getElementById('anchor-bucket-id').value = bucketId;
  document.getElementById('anchor-bucket-search').value = bucketName;
  document.getElementById('anchor-bucket-selected').innerHTML = 
    `✓ 已选择: <b>${esc(bucketName)}</b> (#${bucketId.substring(0, 8)})`;
  document.getElementById('anchor-bucket-selected').style.display = 'block';
  document.getElementById('anchor-bucket-suggestions').style.display = 'none';
}

function updateEmotionDot() {
  const valence = parseFloat(document.getElementById('anchor-valence').value);
  const arousal = parseFloat(document.getElementById('anchor-arousal').value);
  const dot = document.getElementById('anchor-emotion-dot');
  if (dot) {
    dot.style.left = (valence * 100) + '%';
    dot.style.top = ((1 - arousal) * 100) + '%';
  }
}

function closeAnchorEditor() {
  document.getElementById('anchor-modal').style.display = 'none';
}

async function saveAnchor() {
  const bucketId = document.getElementById('anchor-bucket-id').value.trim();
  const intensity = parseFloat(document.getElementById('anchor-intensity').value);
  const valence = parseFloat(document.getElementById('anchor-valence').value);
  const arousal = parseFloat(document.getElementById('anchor-arousal').value);
  const summary = document.getElementById('anchor-summary').value.trim();
  const emotionTags = getSelectedEmotionTags();
  
  if (!bucketId) {
    alert('请选择关联的记忆桶');
    return;
  }
  if (!summary) {
    alert('请填写事件摘要');
    return;
  }
  
  try {
    const resp = await authFetch('/api/anchors', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        bucket_id: bucketId,
        emotion_intensity: intensity,
        summary: summary,
        coordinates: { valence, arousal },
        emotion_tags: emotionTags.map(t => t.label)
      })
    });
    if (!resp) return;
    
    closeAnchorEditor();
    loadAnchors();
  } catch(e) {
    alert('保存失败: ' + e.message);
  }
}

async function deleteAnchor(anchorId) {
  if (!confirm('确定要删除这条锚点吗？')) return;
  
  try {
    const resp = await authFetch(`/api/anchors/${anchorId}`, {
      method: 'DELETE'
    });
    if (!resp) return;
    
    loadAnchors();
  } catch(e) {
    alert('删除失败: ' + e.message);
  }
}

async function autoCreateAnchors() {
  const thresholdInput = prompt('请输入情绪强度阈值 (0-1，建议值0.5-0.7):', '0.6');
  if (thresholdInput === null) return;
  
  const threshold = parseFloat(thresholdInput);
  if (isNaN(threshold) || threshold < 0 || threshold > 1) {
    alert('请输入有效的阈值（0到1之间的数字）');
    return;
  }
  
  const list = document.getElementById('anchor-list');
  const loadingHtml = `
    <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;padding:60px 20px;">
      <div style="width:48px;height:48px;border:4px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin 1s linear infinite;"></div>
      <div style="margin-top:16px;font-size:14px;color:var(--text-dim);">正在扫描记忆桶...</div>
      <div style="margin-top:8px;font-size:12px;color:var(--text-light);" id="auto-anchor-progress">分析中，请稍候...</div>
      <div style="margin-top:12px;width:200px;height:6px;background:var(--border);border-radius:3px;overflow:hidden;">
        <div id="auto-anchor-bar" style="height:100%;width:0%;background:var(--accent);transition:width 0.3s;"></div>
      </div>
    </div>
    <style>@keyframes spin { to { transform: rotate(360deg); } }</style>
  `;
  list.innerHTML = loadingHtml;
  
  try {
    const resp = await authFetch('/api/bucket-auto-anchor', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ threshold: threshold })
    });
    if (!resp) {
      loadAnchors();
      return;
    }
    
    const data = await resp.json();
    
    document.getElementById('auto-anchor-bar').style.width = '100%';
    
    let msg = `检测完成！\n\n`;
    msg += `扫描记忆桶: ${data.total_scanned} 个\n`;
    msg += `含情绪数据: ${data.buckets_with_emotions || 0} 个\n`;
    msg += `情绪未达标: ${data.buckets_below_threshold || 0} 个\n`;
    msg += `创建锚点: ${data.anchors_created} 个\n\n`;
    if (data.anchors_created === 0 && data.buckets_with_emotions > 0) {
      msg += `提示：当前阈值${threshold}较高，可尝试降低阈值`;
    } else if (data.buckets_with_emotions === 0) {
      msg += '提示：没有找到带有情绪数据的记忆桶';
    }
    alert(msg);
    loadAnchors();
  } catch(e) {
    alert('自动检测失败: ' + e.message);
    loadAnchors();
  }
}

document.getElementById('anchor-intensity').addEventListener('input', function(e) {
  const val = parseFloat(e.target.value);
  document.getElementById('anchor-intensity-value').textContent = val.toFixed(2);
  document.getElementById('anchor-intensity-input').value = val.toFixed(2);
});
document.getElementById('anchor-intensity-input').addEventListener('input', function(e) {
  let val = parseFloat(e.target.value);
  if (isNaN(val)) val = 0;
  val = Math.max(0, Math.min(1, val));
  document.getElementById('anchor-intensity').value = val;
  document.getElementById('anchor-intensity-value').textContent = val.toFixed(2);
});

document.getElementById('anchor-valence').addEventListener('input', function(e) {
  const val = parseFloat(e.target.value);
  document.getElementById('anchor-valence-value').textContent = val.toFixed(2);
  document.getElementById('anchor-valence-input').value = val.toFixed(2);
  updateEmotionDot();
});
document.getElementById('anchor-valence-input').addEventListener('input', function(e) {
  let val = parseFloat(e.target.value);
  if (isNaN(val)) val = 0.5;
  val = Math.max(0, Math.min(1, val));
  document.getElementById('anchor-valence').value = val;
  document.getElementById('anchor-valence-value').textContent = val.toFixed(2);
  updateEmotionDot();
});

document.getElementById('anchor-arousal').addEventListener('input', function(e) {
  const val = parseFloat(e.target.value);
  document.getElementById('anchor-arousal-value').textContent = val.toFixed(2);
  document.getElementById('anchor-arousal-input').value = val.toFixed(2);
  updateEmotionDot();
});
document.getElementById('anchor-arousal-input').addEventListener('input', function(e) {
  let val = parseFloat(e.target.value);
  if (isNaN(val)) val = 0.5;
  val = Math.max(0, Math.min(1, val));
  document.getElementById('anchor-arousal').value = val;
  document.getElementById('anchor-arousal-value').textContent = val.toFixed(2);
  updateEmotionDot();
});

// ========================================
// Candlestick / 烛台功能
// ========================================

function renderCandlesticks(candlesticks) {
  const stats = document.getElementById('candlestick-stats');
  const list = document.getElementById('candlestick-list');
  const empty = document.getElementById('candlestick-empty');
  
  stats.innerHTML = '';
  list.innerHTML = '';
  
  stats.innerHTML = `
    <div style="background:linear-gradient(135deg,#FFB74D15,#FFB74D08);border-radius:14px;padding:18px 24px;border:1px solid #FFB74D20;display:flex;align-items:center;gap:14px;">
      <div style="width:44px;height:44px;border-radius:12px;background:#FFB74D;display:flex;align-items:center;justify-content:center;color:#fff;font-size:18px;font-weight:600;">🕯️</div>
      <div>
        <div style="font-size:14px;font-weight:500;">总感想</div>
        <div style="font-size:11px;color:var(--text-dim);">${candlesticks.length} 条感悟记录</div>
      </div>
    </div>
  `;
  
  if (candlesticks.length === 0) {
    empty.style.display = 'block';
    return;
  }
  empty.style.display = 'none';
  
  candlesticks.forEach(candle => {
    const card = `
      <div style="background:var(--surface);border-radius:16px;padding:20px;border:1px solid var(--border);margin-bottom:14px;transition:all 0.2s;border-left:4px solid #FFB74D;">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px;">
          <div>
            ${candle.title ? `<h3 style="margin:0;font-size:17px;font-weight:600;color:var(--text);">${escapeHtml(candle.title)}</h3>` : ''}
            <div style="font-size:12px;color:var(--text-dim);margin-top:4px;">${candle.created ? new Date(candle.created).toLocaleString() : ''}</div>
          </div>
          <div style="display:flex;gap:6px;">
            ${candle.bucket_id ? `<button onclick="showDetail('${candle.bucket_id}')" style="padding:6px 14px;border-radius:10px;border:1px solid var(--border);background:var(--surface);cursor:pointer;font-size:12px;color:var(--text-secondary);transition:all 0.2s;">查看记忆</button>` : ''}
            <button onclick="deleteCandlestick('${candle.id}')" style="padding:6px 14px;border-radius:10px;border:none;background:#FF6B6B15;color:#FF6B6B;cursor:pointer;font-size:12px;font-weight:500;transition:all 0.2s;">删除</button>
          </div>
        </div>
        <div style="font-size:14px;color:var(--text-secondary);line-height:1.7;white-space:pre-wrap;">${escapeHtml(candle.content)}</div>
        ${candle.bucket_id ? `<div style="margin-top:10px;font-size:12px;color:#6A6A8B;display:flex;align-items:center;gap:6px;"><span style="width:6px;height:6px;border-radius:50%;background:#6A6A8B;"></span>关联记忆桶: ${candle.bucket_id}</div>` : ''}
      </div>
    `;
    list.innerHTML += card;
  });
}

async function loadCandlesticks() {
  const list = document.getElementById('candlestick-list');
  try {
    const cached = getCachedData('candlesticks');
    if (cached) {
      renderCandlesticks(cached);
      return;
    }
    
    const resp = await authFetch('/api/candlesticks');
    if (!resp) return;
    const data = await resp.json();
    const candlesticks = data.candlesticks || [];
    setCachedData('candlesticks', candlesticks);
    renderCandlesticks(candlesticks);
  } catch(e) {
    list.innerHTML = `<div class="loading">加载失败: ${e.message}</div>`;
  }
}

function showCandlestickEditor() {
  document.getElementById('candlestick-edit-id').value = '';
  document.getElementById('candlestick-title').value = '';
  document.getElementById('candlestick-bucket-id').value = '';
  document.getElementById('candlestick-content').value = '';
  document.getElementById('candlestick-modal-title').textContent = '记录感想';
  document.getElementById('candlestick-modal').style.display = 'flex';
}

function closeCandlestickEditor() {
  document.getElementById('candlestick-modal').style.display = 'none';
}

async function saveCandlestick() {
  const title = document.getElementById('candlestick-title').value;
  const bucketId = document.getElementById('candlestick-bucket-id').value;
  const content = document.getElementById('candlestick-content').value;
  
  if (!content.trim()) {
    alert('请填写感悟内容');
    return;
  }
  
  try {
    const resp = await authFetch('/api/candlesticks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title: title,
        bucket_id: bucketId,
        content: content
      })
    });
    if (!resp) return;
    
    closeCandlestickEditor();
    invalidateCache('candlesticks');
    loadCandlesticks();
  } catch(e) {
    alert('保存失败: ' + e.message);
  }
}

async function deleteCandlestick(candlestickId) {
  if (!confirm('确定要删除这条感想吗？')) return;
  
  try {
    const resp = await authFetch(`/api/candlesticks/${candlestickId}`, {
      method: 'DELETE'
    });
    if (!resp) return;
    
    invalidateCache('candlesticks');
    loadCandlesticks();
  } catch(e) {
    alert('删除失败: ' + e.message);
  }
}

// ========================================
// Analytics / 数据分析功能
// ========================================

function renderAnalyticsInDirectory(analytics, container) {
  if (!analytics || analytics.total_buckets === 0) return;

  const recentActivity = analytics.recent_activity || [];
  const maxActivity = recentActivity.length > 0 ? Math.max(...recentActivity.map(d => d.count), 1) : 1;
  
  const activityHtml = recentActivity.slice(-7).map(d => `
    <div style="text-align:center;">
      <div style="height:30px;width:18px;background:var(--border);border-radius:3px;position:relative;overflow:hidden;margin-bottom:3px;">
        <div style="position:absolute;bottom:0;left:0;width:100%;background:var(--accent);height:${(d.count / maxActivity) * 100}%;"></div>
      </div>
      <div style="font-size:9px;color:var(--text-dim);">${d.date ? d.date.slice(5) : '-'}</div>
    </div>
  `).join('');

  const avgValence = analytics.avg_valence !== undefined ? analytics.avg_valence : 0.5;
  const avgArousal = analytics.avg_arousal !== undefined ? analytics.avg_arousal : 0.3;
  const valenceStatus = avgValence >= 0.7 ? '正面' : avgValence >= 0.3 ? '中性' : '负面';
  const arousalStatus = avgArousal >= 0.7 ? '激动' : avgArousal >= 0.3 ? '中等' : '平静';

  const domainHtml = '';
  const domains = analytics.domain_counts || {};
  for (const [domain, count] of Object.entries(domains)) {
    domainHtml += `
      <span style="background:#6A6A8B15;color:#6A6A8B;padding:4px 10px;border-radius:10px;font-size:12px;">${domain} (${count})</span>
    `;
  }

  container.innerHTML += `
    <div style="margin-top:24px;padding-top:24px;border-top:1px solid var(--border);">
      <h3 style="margin:0 0 16px 0;font-size:16px;">📊 数据统计</h3>
      
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:20px;">
        <div style="background:var(--surface);border-radius:12px;padding:14px;border:1px solid var(--border);">
          <div style="font-size:12px;color:var(--text-dim);margin-bottom:4px;">情绪效价</div>
          <div style="display:flex;align-items:center;gap:8px;">
            <div style="width:60px;height:6px;background:var(--border);border-radius:3px;overflow:hidden;">
              <div style="width:${avgValence * 100}%;height:100%;background:linear-gradient(90deg,#EF5350,#4CAF50);"></div>
            </div>
            <span style="font-size:14px;font-weight:500;">${valenceStatus}</span>
          </div>
        </div>
        <div style="background:var(--surface);border-radius:12px;padding:14px;border:1px solid var(--border);">
          <div style="font-size:12px;color:var(--text-dim);margin-bottom:4px;">情绪唤醒度</div>
          <div style="display:flex;align-items:center;gap:8px;">
            <div style="width:60px;height:6px;background:var(--border);border-radius:3px;overflow:hidden;">
              <div style="width:${avgArousal * 100}%;height:100%;background:linear-gradient(90deg,#BBDEFB,#FFB74D);"></div>
            </div>
            <span style="font-size:14px;font-weight:500;">${arousalStatus}</span>
          </div>
        </div>
        <div style="background:var(--surface);border-radius:12px;padding:14px;border:1px solid var(--border);">
          <div style="font-size:12px;color:var(--text-dim);margin-bottom:4px;">总记录数</div>
          <div style="font-size:20px;font-weight:bold;color:var(--accent);">${analytics.total_buckets || 0}</div>
        </div>
        <div style="background:var(--surface);border-radius:12px;padding:14px;border:1px solid var(--border);">
          <div style="font-size:12px;color:var(--text-dim);margin-bottom:4px;">活跃天数</div>
          <div style="font-size:20px;font-weight:bold;color:#4CAF50;">${analytics.active_days || 0}</div>
        </div>
      </div>

      ${activityHtml ? `
        <div style="background:var(--surface);border-radius:12px;padding:14px;border:1px solid var(--border);margin-bottom:16px;">
          <div style="font-size:12px;color:var(--text-dim);margin-bottom:10px;">近7天活跃度</div>
          <div style="display:flex;justify-content:space-between;align-items:flex-end;">${activityHtml}</div>
        </div>
      ` : ''}

      ${analytics.month_data && analytics.month_data.length > 0 ? renderEmotionCalendar(analytics) : ''}
    </div>
  `;
}

function renderEmotionCalendar(analytics) {
  const today = new Date();
  const currentMonth = today.toISOString().split('T')[0].slice(0, 7);
  
  const emotionMap = {};
  (analytics.month_data || []).forEach(m => {
    emotionMap[m.month] = m;
  });
  
  const selectedMonthData = emotionMap[currentMonth] || { positive: 0, negative: 0, count: 0 };
  
  const [year, month] = currentMonth.split('-');
  const firstDay = new Date(parseInt(year), parseInt(month) - 1, 1);
  const lastDay = new Date(parseInt(year), parseInt(month), 0);
  const daysInMonth = lastDay.getDate();
  const startDay = firstDay.getDay();
  
  const weekdays = ['日', '一', '二', '三', '四', '五', '六'];
  
  const dayEmotions = {};
  if (analytics.emotion_dates) {
    analytics.emotion_dates.forEach(d => {
      if (d.date.startsWith(currentMonth)) {
        dayEmotions[d.date] = d;
      }
    });
  }
  
  let calendarCells = '';
  for (let i = 0; i < startDay; i++) {
    calendarCells += `<div style="aspect-ratio:1;"></div>`;
  }
  
  let maxDayIntensity = 1;
  for (let day = 1; day <= daysInMonth; day++) {
    const dateStr = `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
    const dayData = dayEmotions[dateStr] || { positive: 0, negative: 0, count: 0 };
    maxDayIntensity = Math.max(maxDayIntensity, dayData.positive, dayData.negative);
  }
  
  for (let day = 1; day <= daysInMonth; day++) {
    const dateStr = `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
    const dayData = dayEmotions[dateStr] || { positive: 0, negative: 0, count: 0 };
    
    const posRatio = Math.min(dayData.positive / maxDayIntensity, 1);
    const negRatio = Math.min(dayData.negative / maxDayIntensity, 1);
    const posAlpha = posRatio * 0.5 + 0.15;
    const negAlpha = negRatio * 0.5 + 0.15;
    
    let bgStyle = '';
    if (posRatio > 0 && negRatio > 0) {
      bgStyle = `background:linear-gradient(135deg,rgba(76,175,80,${posAlpha}) 50%,rgba(211,47,47,${negAlpha}) 50%);`;
    } else if (posRatio > 0) {
      bgStyle = `background:rgba(76,175,80,${posAlpha});`;
    } else if (negRatio > 0) {
      bgStyle = `background:rgba(211,47,47,${negAlpha});`;
    } else {
      bgStyle = 'background:var(--border);';
    }
    
    const isToday = dateStr === today.toISOString().split('T')[0];
    
    calendarCells += `
      <div ${bgStyle} style="aspect-ratio:1;border-radius:3px;display:flex;flex-direction:column;justify-content:center;align-items:center;cursor:pointer;position:relative;"
           title="${dateStr}: 正面${dayData.positive} | 负面${dayData.negative}">
        ${isToday ? '<div style="position:absolute;top:0.5px;right:0.5px;width:2px;height:2px;background:white;border-radius:50%;"></div>' : ''}
        <div style="font-size:9px;font-weight:500;color:${dayData.count > 0 ? 'white' : 'var(--text-dim)'};text-shadow:${dayData.count > 0 ? '0 1px 1px rgba(0,0,0,0.2)' : 'none'};">${day}</div>
      </div>
    `;
  }
  
  return `
    <div style="background:var(--surface);border-radius:12px;padding:14px;border:1px solid var(--border);">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
        <div style="font-size:12px;color:var(--text-dim);">情绪热力图</div>
        <div style="display:flex;align-items:center;gap:4px;font-size:10px;color:var(--text-light);">
          <div style="width:12px;height:12px;border-radius:2px;background:rgba(76,175,80,0.5);"></div> 正面
          <div style="width:12px;height:12px;border-radius:2px;background:rgba(211,47,47,0.5);"></div> 负面
        </div>
      </div>
      <div style="display:grid;grid-template-columns:repeat(7,1fr);gap:2px;">
        ${weekdays.map(w => `<div style="text-align:center;padding:1px 0;font-size:8px;color:var(--text-dim);font-weight:500;">${w}</div>`).join('')}
        ${calendarCells}
      </div>
    </div>
  `;
}

function renderAnalytics(data) {
  _analyticsData = data;
  const content = document.getElementById('analytics-content');
  
  const typeColors = {
    dynamic: '#2F4F4F',
    permanent: '#9A7B4F',
    feel: '#8B6A6A',
    identity: '#4A7C59',
    pattern: '#6A6A8B',
    experience: '#6A6A8B',
  };
  
  const typeLabels = {
    dynamic: '动态记忆',
    permanent: '永久记忆',
    feel: '感受',
    identity: '身份',
    pattern: '模式',
    experience: '经验',
  };
  
  if (!data || data.total_buckets === 0) {
    content.innerHTML = `
      <div style="text-align:center;padding:60px;color:var(--text-dim);">
        <div style="font-size:48px;margin-bottom:16px;">📊</div>
        <div style="font-size:18px;margin-bottom:8px;">暂无数据</div>
        <div style="font-size:13px;">添加一些记忆后，数据分析将自动生成</div>
      </div>
    `;
    return;
  }
  
  let domainHtml = '';
  const domains = data.domain_counts || {};
  for (const [domain, count] of Object.entries(domains)) {
    domainHtml += `
      <span style="background:#6A6A8B15;color:#6A6A8B;padding:6px 12px;border-radius:12px;font-size:13px;">${domain} (${count})</span>
    `;
  }
  if (!domainHtml) {
    domainHtml = '<span style="color:var(--text-light);font-size:13px;">暂无主题数据</span>';
  }
  
  const recentActivity = data.recent_activity || [];
  const maxActivity = recentActivity.length > 0 ? Math.max(...recentActivity.map(d => d.count), 1) : 1;
  const activityHtml = recentActivity.map(d => `
    <div style="text-align:center;">
      <div style="height:40px;width:20px;background:var(--border);border-radius:4px;position:relative;overflow:hidden;margin-bottom:4px;">
        <div style="position:absolute;bottom:0;left:0;width:100%;background:var(--accent);height:${(d.count / maxActivity) * 100}%;"></div>
      </div>
      <div style="font-size:10px;color:var(--text-dim);">${d.date ? d.date.slice(5) : '-'}</div>
    </div>
  `).join('');
  
  const avgValence = data.avg_valence !== undefined ? data.avg_valence : 0.5;
  const avgArousal = data.avg_arousal !== undefined ? data.avg_arousal : 0.3;
  const valenceStatus = avgValence >= 0.7 ? '正面' : avgValence >= 0.3 ? '中性' : '负面';
  const arousalStatus = avgArousal >= 0.7 ? '激动' : avgArousal >= 0.3 ? '中等' : '平静';
  
  const maxIntensity = data.max_emotion_intensity || 1;
  const emotionDates = data.emotion_dates || [];
  
  let emotionTableHtml = '';
  const monthData = data.month_data || [];
  
  if (monthData.length > 0) {
    const today = new Date();
    const currentMonth = today.toISOString().split('T')[0].slice(0, 7);
    
    const emotionMap = {};
    monthData.forEach(m => {
      emotionMap[m.month] = m;
    });
    
    const selectedMonthData = emotionMap[currentMonth] || { positive: 0, negative: 0, count: 0 };
    
    const [year, month] = currentMonth.split('-');
    const firstDay = new Date(parseInt(year), parseInt(month) - 1, 1);
    const lastDay = new Date(parseInt(year), parseInt(month), 0);
    const daysInMonth = lastDay.getDate();
    const startDay = firstDay.getDay();
    
    const weekdays = ['日', '一', '二', '三', '四', '五', '六'];
    const monthNames = ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月'];
    
    const dayEmotions = {};
    if (data.emotion_dates) {
      data.emotion_dates.forEach(d => {
        if (d.date.startsWith(currentMonth)) {
          dayEmotions[d.date] = d;
        }
      });
    }
    
    let calendarCells = '';
    for (let i = 0; i < startDay; i++) {
      calendarCells += `<div style="aspect-ratio:1;"></div>`;
    }
    
    let totalRecords = 0;
    let maxDayIntensity = 1;
    
    for (let day = 1; day <= daysInMonth; day++) {
      const dateStr = `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
      const dayData = dayEmotions[dateStr] || { positive: 0, negative: 0, count: 0 };
      totalRecords += dayData.count;
      maxDayIntensity = Math.max(maxDayIntensity, dayData.positive, dayData.negative);
      
      const posRatio = Math.min(dayData.positive / maxDayIntensity, 1);
      const negRatio = Math.min(dayData.negative / maxDayIntensity, 1);
      const posAlpha = posRatio * 0.6 + 0.15;
      const negAlpha = negRatio * 0.6 + 0.15;
      
      let bgStyle = '';
      if (posRatio > 0 && negRatio > 0) {
        bgStyle = `background:linear-gradient(135deg,rgba(76,175,80,${posAlpha}) 50%,rgba(211,47,47,${negAlpha}) 50%);`;
      } else if (posRatio > 0) {
        bgStyle = `background:rgba(76,175,80,${posAlpha});`;
      } else if (negRatio > 0) {
        bgStyle = `background:rgba(211,47,47,${negAlpha});`;
      } else {
        bgStyle = 'background:var(--border);';
      }
      
      const isToday = dateStr === today.toISOString().split('T')[0];
      
      calendarCells += `
        <div ${bgStyle} style="aspect-ratio:1;border-radius:4px;display:flex;flex-direction:column;justify-content:center;align-items:center;cursor:pointer;position:relative;"
             title="${dateStr}: 正面${dayData.positive} | 负面${dayData.negative} | ${dayData.count}条记录">
          ${isToday ? '<div style="position:absolute;top:1px;right:1px;width:3px;height:3px;background:white;border-radius:50%;"></div>' : ''}
          <div style="font-size:10px;font-weight:500;color:${dayData.count > 0 ? 'white' : 'var(--text-dim)'};text-shadow:${dayData.count > 0 ? '0 1px 1px rgba(0,0,0,0.2)' : 'none'};">${day}</div>
        </div>
      `;
    }
    
    const completionRate = daysInMonth > 0 ? Math.round((totalRecords / daysInMonth) * 100) : 0;
    const statusText = completionRate >= 80 ? '非常出色！' : completionRate >= 50 ? '继续加油！' : '开始记录吧！';
    
    emotionTableHtml = `
      <div style="background:var(--surface);border-radius:12px;padding:16px;border:1px solid var(--border);">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
          <h3 style="margin:0;font-size:14px;font-weight:600;color:var(--text);">情绪热力图</h3>
          <div style="display:flex;align-items:center;gap:6px;">
            <button onclick="changeMonth(-1)" style="width:24px;height:24px;border-radius:50%;border:1px solid var(--border);background:var(--surface);color:var(--text-dim);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:12px;">‹</button>
            <div style="font-size:13px;font-weight:500;color:var(--text);">${year}年${monthNames[parseInt(month)-1]}</div>
            <button onclick="changeMonth(1)" style="width:24px;height:24px;border-radius:50%;border:1px solid var(--border);background:var(--surface);color:var(--text-dim);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:12px;">›</button>
          </div>
        </div>
        <div style="display:grid;grid-template-columns:repeat(7,1fr);gap:2px;margin-bottom:12px;">
          ${weekdays.map(w => `<div style="text-align:center;padding:2px 0;font-size:9px;color:var(--text-dim);font-weight:500;">${w}</div>`).join('')}
          ${calendarCells}
        </div>
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <div>
            <div style="font-size:20px;font-weight:bold;color:var(--accent);">${completionRate}%</div>
            <div style="font-size:11px;color:var(--text-light);">本月 ${totalRecords} 条</div>
          </div>
          <div style="display:flex;align-items:center;gap:10px;font-size:10px;color:var(--text-dim);">
            <div style="display:flex;align-items:center;gap:3px;"><div style="width:10px;height:10px;border-radius:3px;background:rgba(76,175,80,0.6);"></div><span>正面</span></div>
            <div style="display:flex;align-items:center;gap:3px;"><div style="width:10px;height:10px;border-radius:3px;background:rgba(211,47,47,0.6);"></div><span>负面</span></div>
            <div style="display:flex;align-items:center;gap:3px;"><div style="width:10px;height:10px;border-radius:3px;background:var(--border);"></div><span>无</span></div>
          </div>
        </div>
      </div>
    `;
  } else {
    const today = new Date();
    const [year, month] = today.toISOString().split('T')[0].slice(0, 7).split('-');
    const firstDay = new Date(parseInt(year), parseInt(month) - 1, 1);
    const lastDay = new Date(parseInt(year), parseInt(month), 0);
    const daysInMonth = lastDay.getDate();
    const startDay = firstDay.getDay();
    const weekdays = ['日', '一', '二', '三', '四', '五', '六'];
    
    let calendarCells = '';
    for (let i = 0; i < startDay; i++) {
      calendarCells += `<div style="aspect-ratio:1;"></div>`;
    }
    for (let day = 1; day <= daysInMonth; day++) {
      calendarCells += `<div style="aspect-ratio:1;border-radius:8px;background:var(--border);display:flex;flex-direction:column;justify-content:center;align-items:center;"><div style="font-size:12px;color:var(--text-dim);">${day}</div></div>`;
    }
    
    emotionTableHtml = `
      <div style="background:var(--surface);border-radius:16px;padding:24px;border:1px solid var(--border);">
        <h3 style="margin:0 0 16px 0;font-size:16px;">情绪热力图</h3>
        <div style="display:flex;justify-content:center;margin-bottom:16px;">
          <div style="font-size:18px;font-weight:600;color:var(--text);">${year}年${parseInt(month)}月</div>
        </div>
        <div style="display:grid;grid-template-columns:repeat(7,1fr);gap:4px;margin-bottom:20px;">
          ${weekdays.map(w => `<div style="text-align:center;padding:6px 0;font-size:11px;color:var(--text-dim);font-weight:500;">${w}</div>`).join('')}
          ${calendarCells}
        </div>
        <div style="text-align:center;padding:16px;color:var(--text-light);font-size:13px;">暂无情绪数据</div>
      </div>
    `;
  }
  
  const typeCounts = data.type_counts || {};
  const typeHtml = Object.entries(typeCounts).map(([type, count]) => `
    <div style="background:var(--surface);border-radius:16px;padding:20px;text-align:center;border:1px solid ${typeColors[type] || '#6A6A8B'}30;">
      <div style="font-size:32px;font-weight:bold;color:${typeColors[type] || '#6A6A8B'};">${count}</div>
      <div style="font-size:13px;color:var(--text-dim);margin-top:4px;">${typeLabels[type] || type}</div>
    </div>
  `).join('');
  
  content.innerHTML = `
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:20px;">
      <div style="background:var(--surface);border-radius:16px;padding:20px;text-align:center;border:1px solid var(--border);">
        <div style="font-size:32px;font-weight:bold;color:var(--accent);">${data.total_buckets}</div>
        <div style="font-size:13px;color:var(--text-dim);margin-top:4px;">总记忆桶</div>
      </div>
      ${typeHtml}
    </div>
    
    ${emotionTableHtml}
    
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:20px;">
      <div style="background:var(--surface);border-radius:16px;padding:20px;border:1px solid var(--border);">
        <div style="font-size:12px;color:var(--text-dim);margin-bottom:8px;">平均效价</div>
        <div style="font-size:28px;font-weight:bold;color:${avgValence >= 0.5 ? '#4CAF50' : '#C62828'};">${avgValence}</div>
        <div style="font-size:11px;color:var(--text-light);margin-top:4px;">${valenceStatus}</div>
      </div>
      <div style="background:var(--surface);border-radius:16px;padding:20px;border:1px solid var(--border);">
        <div style="font-size:12px;color:var(--text-dim);margin-bottom:8px;">平均唤醒度</div>
        <div style="font-size:28px;font-weight:bold;color:${avgArousal >= 0.5 ? '#FFB74D' : '#6A6A8B'};">${avgArousal}</div>
        <div style="font-size:11px;color:var(--text-light);margin-top:4px;">${arousalStatus}</div>
      </div>
    </div>
    
    <div style="display:grid;grid-template-columns:1fr;gap:20px;margin-bottom:20px;">
      <div style="background:var(--surface);border-radius:16px;padding:24px;border:1px solid var(--border);">
        <h3 style="margin:0 0 16px 0;font-size:16px;">热门主题</h3>
        <div style="display:flex;flex-wrap:wrap;gap:8px;">
          ${domainHtml}
        </div>
      </div>
      
      <div style="background:var(--surface);border-radius:16px;padding:24px;border:1px solid var(--border);">
        <h3 style="margin:0 0 16px 0;font-size:16px;">近期活跃度（近7天）</h3>
        <div style="display:flex;justify-content:space-around;align-items:flex-end;height:80px;">
          ${activityHtml}
        </div>
      </div>
    </div>
  `;
}

function getWeekday(dateStr) {
  const date = new Date(dateStr);
  const weekdays = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
  return weekdays[date.getDay()];
}

let _analyticsData = null;

function changeMonth(delta) {
  if (!_analyticsData) return;
  const today = new Date();
  const currentMonth = today.toISOString().split('T')[0].slice(0, 7);
  const [year, month] = currentMonth.split('-');
  const newDate = new Date(parseInt(year), parseInt(month) - 1 + delta, 1);
  const newMonthStr = newDate.toISOString().split('T')[0].slice(0, 7);
  
  const emotionMap = {};
  if (_analyticsData.emotion_dates) {
    _analyticsData.emotion_dates.forEach(d => {
      if (d.date.startsWith(newMonthStr)) {
        emotionMap[d.date] = d;
      }
    });
  }
  
  const [newYear, newMonth] = newMonthStr.split('-');
  const firstDay = new Date(parseInt(newYear), parseInt(newMonth) - 1, 1);
  const lastDay = new Date(parseInt(newYear), parseInt(newMonth), 0);
  const daysInMonth = lastDay.getDate();
  const startDay = firstDay.getDay();
  
  const weekdays = ['日', '一', '二', '三', '四', '五', '六'];
  const monthNames = ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月'];
  
  let calendarCells = '';
  for (let i = 0; i < startDay; i++) {
    calendarCells += `<div style="aspect-ratio:1;"></div>`;
  }
  
  let totalRecords = 0;
  let maxDayIntensity = 1;
  const monthData = _analyticsData.month_data || [];
  const maxIntensity = _analyticsData.max_emotion_intensity || 1;
  
  for (let day = 1; day <= daysInMonth; day++) {
    const dateStr = `${newYear}-${String(newMonth).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
    const dayData = emotionMap[dateStr] || { positive: 0, negative: 0, count: 0 };
    totalRecords += dayData.count;
    maxDayIntensity = Math.max(maxDayIntensity, dayData.positive, dayData.negative);
  }
  
  for (let day = 1; day <= daysInMonth; day++) {
    const dateStr = `${newYear}-${String(newMonth).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
    const dayData = emotionMap[dateStr] || { positive: 0, negative: 0, count: 0 };
    
    const posRatio = Math.min(dayData.positive / maxIntensity, 1);
    const negRatio = Math.min(dayData.negative / maxIntensity, 1);
    const posAlpha = posRatio * 0.6 + 0.15;
    const negAlpha = negRatio * 0.6 + 0.15;
    
    let bgStyle = '';
    if (posRatio > 0 && negRatio > 0) {
      bgStyle = `background:linear-gradient(135deg,rgba(76,175,80,${posAlpha}) 50%,rgba(211,47,47,${negAlpha}) 50%);`;
    } else if (posRatio > 0) {
      bgStyle = `background:rgba(76,175,80,${posAlpha});`;
    } else if (negRatio > 0) {
      bgStyle = `background:rgba(211,47,47,${negAlpha});`;
    } else {
      bgStyle = 'background:var(--border);';
    }
    
    const isToday = dateStr === today.toISOString().split('T')[0];
    
    calendarCells += `
      <div ${bgStyle} style="aspect-ratio:1;border-radius:4px;display:flex;flex-direction:column;justify-content:center;align-items:center;cursor:pointer;position:relative;"
           title="${dateStr}: 正面${dayData.positive} | 负面${dayData.negative} | ${dayData.count}条记录">
        ${isToday ? '<div style="position:absolute;top:1px;right:1px;width:3px;height:3px;background:white;border-radius:50%;"></div>' : ''}
        <div style="font-size:10px;font-weight:500;color:${dayData.count > 0 ? 'white' : 'var(--text-dim)'};text-shadow:${dayData.count > 0 ? '0 1px 1px rgba(0,0,0,0.2)' : 'none'};">${day}</div>
      </div>
    `;
  }
  
  const completionRate = daysInMonth > 0 ? Math.round((totalRecords / daysInMonth) * 100) : 0;
  
  const emotionSection = document.querySelector('#analytics-content > div:nth-child(2)');
  if (emotionSection) {
    emotionSection.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
        <h3 style="margin:0;font-size:14px;font-weight:600;color:var(--text);">情绪热力图</h3>
        <div style="display:flex;align-items:center;gap:6px;">
          <button onclick="changeMonth(-1)" style="width:24px;height:24px;border-radius:50%;border:1px solid var(--border);background:var(--surface);color:var(--text-dim);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:12px;">‹</button>
          <div style="font-size:13px;font-weight:500;color:var(--text);">${newYear}年${monthNames[parseInt(newMonth)-1]}</div>
          <button onclick="changeMonth(1)" style="width:24px;height:24px;border-radius:50%;border:1px solid var(--border);background:var(--surface);color:var(--text-dim);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:12px;">›</button>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:repeat(7,1fr);gap:2px;margin-bottom:12px;">
        ${weekdays.map(w => `<div style="text-align:center;padding:2px 0;font-size:9px;color:var(--text-dim);font-weight:500;">${w}</div>`).join('')}
        ${calendarCells}
      </div>
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <div>
          <div style="font-size:20px;font-weight:bold;color:var(--accent);">${completionRate}%</div>
          <div style="font-size:11px;color:var(--text-light);">本月 ${totalRecords} 条</div>
        </div>
        <div style="display:flex;align-items:center;gap:10px;font-size:10px;color:var(--text-dim);">
          <div style="display:flex;align-items:center;gap:3px;"><div style="width:10px;height:10px;border-radius:3px;background:rgba(76,175,80,0.6);"></div><span>正面</span></div>
          <div style="display:flex;align-items:center;gap:3px;"><div style="width:10px;height:10px;border-radius:3px;background:rgba(211,47,47,0.6);"></div><span>负面</span></div>
          <div style="display:flex;align-items:center;gap:3px;"><div style="width:10px;height:10px;border-radius:3px;background:var(--border);"></div><span>无</span></div>
        </div>
      </div>
    `;
  }
}

let currentExpFilter = '';

function renderExperiences(data) {
  const stats = document.getElementById('experience-stats');
  const list = document.getElementById('experience-list');
  const empty = document.getElementById('experience-empty');
  
  stats.innerHTML = '';
  list.innerHTML = '';
  
  const userCount = data.filter(e => e.exp_type === 'user').length;
  const agentCount = data.filter(e => e.exp_type === 'agent').length;
  
  stats.innerHTML = `
    <div style="background:linear-gradient(135deg,#6A6A8B15,#6A6A8B08);border-radius:14px;padding:18px 24px;border:1px solid #6A6A8B20;display:flex;align-items:center;gap:14px;">
      <div style="width:44px;height:44px;border-radius:12px;background:#6A6A8B;display:flex;align-items:center;justify-content:center;color:#fff;font-size:18px;font-weight:600;">${data.length}</div>
      <div>
        <div style="font-size:14px;font-weight:500;">总经验</div>
        <div style="font-size:11px;color:var(--text-dim);">${userCount} 用户 · ${agentCount} 智能体</div>
      </div>
    </div>
  `;
  
  if (data.length === 0) {
    empty.style.display = 'block';
    return;
  }
  empty.style.display = 'none';
  
  data.forEach(exp => {
    const typeLabel = exp.exp_type === 'user' ? '用户经验' : '智能体经验';
    const typeColor = exp.exp_type === 'user' ? '#4A7C59' : '#9A7B4F';
    const typeBg = exp.exp_type === 'user' ? '#4A7C5915' : '#9A7B4F15';
    const tagsHtml = exp.tags && exp.tags.length > 0 ? exp.tags.map(t => `<span style="background:#6A6A8B15;color:#6A6A8B;padding:3px 10px;border-radius:12px;font-size:11px;margin-right:6px;">#${t}</span>`).join('') : '';
    
    const card = `
      <div style="background:var(--surface);border-radius:16px;padding:20px;border:1px solid var(--border);margin-bottom:14px;transition:all 0.2s;hover:border-color:${typeColor}40;hover:box-shadow:0 4px 20px rgba(0,0,0,0.05);">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px;">
          <div>
            <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px;">
              <span style="font-size:17px;font-weight:600;color:var(--text);">${exp.title}</span>
              <span style="font-size:11px;color:${typeColor};background:${typeBg};padding:3px 10px;border-radius:12px;font-weight:500;">${typeLabel}</span>
            </div>
            <div style="font-size:12px;color:var(--text-dim);">更新: ${exp.updated ? new Date(exp.updated).toLocaleString() : ''} | 创建: ${exp.created ? new Date(exp.created).toLocaleString() : ''}</div>
          </div>
          <div style="display:flex;gap:6px;">
            <button onclick="applyExperience('${exp.id}')" style="padding:6px 14px;border-radius:10px;border:none;background:var(--accent);color:white;cursor:pointer;font-size:12px;font-weight:500;transition:all 0.2s;hover:background:var(--accent-hover);">应用</button>
            <button onclick="editExperience('${exp.id}')" style="padding:6px 14px;border-radius:10px;border:1px solid var(--border);background:var(--surface);cursor:pointer;font-size:12px;color:var(--text-secondary);transition:all 0.2s;hover:background:var(--border);">编辑</button>
            <button onclick="deleteExperience('${exp.id}')" style="padding:6px 14px;border-radius:10px;border:none;background:#FF6B6B15;color:#FF6B6B;cursor:pointer;font-size:12px;font-weight:500;transition:all 0.2s;hover:background:#FF6B6B25;">删除</button>
          </div>
        </div>
        <div style="font-size:14px;color:var(--text-secondary);line-height:1.7;margin-bottom:10px;">${exp.content}</div>
        ${tagsHtml ? `<div style="margin-bottom:8px;">${tagsHtml}</div>` : ''}
        <div style="display:flex;gap:16px;font-size:12px;color:var(--text-dim);">
          ${exp.source ? `<span><span style="width:6px;height:6px;border-radius:50%;background:var(--text-dim);display:inline-block;margin-right:4px;"></span>来源: ${exp.source}</span>` : ''}
          <span><span style="width:6px;height:6px;border-radius:50%;background:var(--positive);display:inline-block;margin-right:4px;"></span>应用次数: ${exp.apply_count || 0}</span>
          ${exp.last_applied ? `<span><span style="width:6px;height:6px;border-radius:50%;background:var(--warning);display:inline-block;margin-right:4px;"></span>最后应用: ${new Date(exp.last_applied).toLocaleString()}</span>` : ''}
        </div>
      </div>
    `;
    list.innerHTML += card;
  });
}

async function loadExperiences() {
  const list = document.getElementById('experience-list');
  try {
    const cached = getCachedData('experiences');
    if (cached) {
      const filtered = currentExpFilter ? cached.filter(e => e.exp_type === currentExpFilter) : cached;
      renderExperiences(filtered);
      return;
    }
    
    const resp = await authFetch('/api/experiences');
    if (!resp) return;
    const data = await resp.json();
    setCachedData('experiences', data);
    const filtered = currentExpFilter ? data.filter(e => e.exp_type === currentExpFilter) : data;
    renderExperiences(filtered);
  } catch(e) {
    list.innerHTML = `<div class="loading">加载失败: ${e.message}</div>`;
  }
}

function filterExperiences(exp_type) {
  currentExpFilter = exp_type;
  document.querySelectorAll('.exp-filter-btn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  loadExperiences();
}

function showExperienceEditor(exp_id = null) {
  document.getElementById('exp-edit-id').value = exp_id || '';
  document.getElementById('exp-title').value = '';
  document.getElementById('exp-type').value = 'user';
  document.getElementById('exp-source').value = '';
  document.getElementById('exp-tags').value = '';
  document.getElementById('exp-content').value = '';
  
  if (exp_id) {
    document.getElementById('exp-modal-title').textContent = '编辑经验';
  } else {
    document.getElementById('exp-modal-title').textContent = '添加经验';
  }
  
  document.getElementById('experience-modal').style.display = 'flex';
}

function closeExperienceEditor() {
  document.getElementById('experience-modal').style.display = 'none';
}

async function saveExperience() {
  const exp_id = document.getElementById('exp-edit-id').value;
  const title = document.getElementById('exp-title').value.trim();
  const exp_type = document.getElementById('exp-type').value;
  const source = document.getElementById('exp-source').value.trim();
  const tagsInput = document.getElementById('exp-tags').value.trim();
  const content = document.getElementById('exp-content').value.trim();
  
  if (!title || !content) {
    alert('请填写标题和内容');
    return;
  }
  
  const tags = tagsInput ? tagsInput.split(',').map(t => t.trim()).filter(t => t) : [];
  
  try {
    if (exp_id) {
      const resp = await authFetch(`/api/experiences/${exp_id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, content, exp_type, source, tags })
      });
      if (!resp) return;
    } else {
      const resp = await authFetch('/api/experiences', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, content, exp_type, source, tags })
      });
      if (!resp) return;
    }
    
    closeExperienceEditor();
    loadExperiences();
  } catch(e) {
    alert('保存失败: ' + e.message);
  }
}

async function applyExperience(exp_id) {
  try {
    const resp = await authFetch(`/api/experiences/${exp_id}/apply`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
    });
    if (resp.ok) {
      loadExperiences();
    } else {
      alert('应用失败');
    }
  } catch (e) {
    console.error('Apply experience failed:', e);
    alert('应用失败');
  }
}

async function editExperience(exp_id) {
  try {
    const resp = await authFetch(`/api/bucket/${exp_id}`);
    if (!resp) return;
    const data = await resp.json();
    const meta = data.metadata || {};
    
    document.getElementById('exp-edit-id').value = exp_id;
    document.getElementById('exp-title').value = meta.title || '';
    document.getElementById('exp-type').value = meta.exp_type || 'user';
    document.getElementById('exp-source').value = meta.source || '';
    document.getElementById('exp-tags').value = (meta.tags || []).join(', ');
    document.getElementById('exp-content').value = data.content || '';
    
    document.getElementById('exp-modal-title').textContent = '编辑经验';
    document.getElementById('experience-modal').style.display = 'flex';
  } catch(e) {
    alert('加载失败: ' + e.message);
  }
}

async function deleteExperience(exp_id) {
  if (!confirm('确定要删除这条经验吗？')) return;
  
  try {
    const resp = await authFetch(`/api/experiences/${exp_id}`, {
      method: 'DELETE'
    });
    if (!resp) return;
    
    loadExperiences();
  } catch(e) {
    alert('删除失败: ' + e.message);
  }
}

function renderIdentities(identities) {
  const list = document.getElementById('identity-list');
  const empty = document.getElementById('identity-empty');
  
  if (identities.length === 0) {
    list.innerHTML = '';
    empty.style.display = '';
    return;
  }
  empty.style.display = 'none';
  list.innerHTML = identities.map(i => `
    <div class="identity-card" style="border-radius:16px;padding:24px;border:1px solid #4A7C59;background:linear-gradient(135deg,#4A7C5910,#4A7C5905);">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:16px;">
        <div>
          <div style="display:flex;align-items:center;gap:8px;">
            <h3 style="margin:0;font-size:18px;">${escapeHtml(i.name || i.topic || '未命名')}</h3>
          </div>
          ${i.aliases && i.aliases.length > 0 ? `<div style="font-size:13px;color:var(--text-dim);margin-top:4px;margin-left:32px;">别名: ${escapeHtml(i.aliases.join(', '))}</div>` : ''}
        </div>
        <div style="display:flex;gap:8px;">
          <button onclick="showIdentityEditor('${i.id}')" style="padding:6px 12px;border:none;background:var(--accent);color:white;border-radius:8px;cursor:pointer;font-size:12px;">编辑</button>
          <button onclick="deleteIdentity('${i.id}')" style="padding:6px 12px;border:none;background:var(--negative);color:white;border-radius:8px;cursor:pointer;font-size:12px;">删除</button>
        </div>
      </div>
      
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px;">
        ${i.gender ? `
          <div style="background:rgba(74,124,89,0.1);border-radius:10px;padding:10px;">
            <div style="font-size:11px;color:var(--text-dim);margin-bottom:2px;">性别</div>
            <div style="font-size:14px;">${escapeHtml(i.gender)}</div>
          </div>
        ` : ''}
        ${i.age ? `
          <div style="background:rgba(74,124,89,0.1);border-radius:10px;padding:10px;">
            <div style="font-size:11px;color:var(--text-dim);margin-bottom:2px;">年龄</div>
            <div style="font-size:14px;">${escapeHtml(i.age)} 岁</div>
          </div>
        ` : ''}
        ${i.occupation ? `
          <div style="background:rgba(74,124,89,0.1);border-radius:10px;padding:10px;">
            <div style="font-size:11px;color:var(--text-dim);margin-bottom:2px;">职业</div>
            <div style="font-size:14px;">${escapeHtml(i.occupation)}</div>
          </div>
        ` : ''}
      </div>
      
      ${i.traits && i.traits.length > 0 ? `
        <div style="margin-bottom:12px;">
          <div style="font-size:12px;color:var(--text-dim);font-weight:500;margin-bottom:6px;">性格特征</div>
          <div>${i.traits.map(t => `<span style="background:#4A7C5920;color:#4A7C59;padding:4px 10px;border-radius:12px;font-size:13px;margin-right:6px;margin-bottom:4px;display:inline-block;">${escapeHtml(t)}</span>`).join('')}</div>
        </div>
      ` : ''}
      
      ${i.interests && i.interests.length > 0 ? `
        <div style="margin-bottom:12px;">
          <div style="font-size:12px;color:var(--text-dim);font-weight:500;margin-bottom:6px;">兴趣爱好</div>
          <div>${i.interests.map(t => `<span style="background:#FFB74D20;color:#FFB74D;padding:4px 10px;border-radius:12px;font-size:13px;margin-right:6px;margin-bottom:4px;display:inline-block;">${escapeHtml(t)}</span>`).join('')}</div>
        </div>
      ` : ''}
      
      ${i.basic_info && Object.keys(i.basic_info).length > 0 ? `
        <div style="margin-bottom:12px;">
          <div style="font-size:12px;color:var(--text-dim);font-weight:500;margin-bottom:6px;">其他信息</div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;">
            ${Object.entries(i.basic_info).map(([k, v]) => `<div style="font-size:13px;"><span style="color:var(--text-dim);">${escapeHtml(k)}:</span> ${escapeHtml(v)}</div>`).join('')}
          </div>
        </div>
      ` : ''}
      
      ${i.relationships && i.relationships.length > 0 ? `
        <div style="margin-bottom:12px;">
          <div style="font-size:12px;color:var(--text-dim);font-weight:500;margin-bottom:6px;">人际关系</div>
          <div style="font-size:13px;color:var(--text-dim);">${i.relationships.map(r => `• ${escapeHtml(r)}`).join('<br>')}</div>
        </div>
      ` : ''}
      
      ${i.notes ? `
        <div style="margin-bottom:12px;">
          <div style="font-size:12px;color:var(--text-dim);font-weight:500;margin-bottom:6px;">备注</div>
          <div style="font-size:13px;color:var(--text-secondary);line-height:1.5;">${escapeHtml(i.notes)}</div>
        </div>
      ` : ''}
      
      <div style="margin-top:16px;padding-top:12px;border-top:1px solid rgba(74,124,89,0.2);font-size:11px;color:var(--text-dim);">
        创建: ${new Date(i.created_at).toLocaleString()} | 更新: ${new Date(i.updated_at).toLocaleString()}
      </div>
    </div>
  `).join('');
}

async function loadIdentities() {
  const list = document.getElementById('identity-list');
  try {
    const resp = await authFetch('/api/identities');
    if (!resp) return;
    const data = await resp.json();
    const identities = data.identities || [];
    renderIdentities(identities);
  } catch(e) {
    list.innerHTML = `<p style="color:var(--negative)">加载失败: ${e.message}</p>`;
  }
}

function renderPatterns(patterns) {
  const list = document.getElementById('pattern-list');
  const empty = document.getElementById('pattern-empty');
  
  if (patterns.length === 0) {
    list.innerHTML = '';
    empty.style.display = '';
    return;
  }
  empty.style.display = 'none';
  list.innerHTML = patterns.map(p => `
    <div class="pattern-card" style="border-radius:12px;padding:16px;border:1px solid #6A6A8B;background:linear-gradient(135deg,#6A6A8B10,#6A6A8B05);">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px;">
        <div>
          <h3 style="margin:0;font-size:16px;">${escapeHtml(p.name || p.topic || '未命名')}</h3>
          ${p.confidence !== undefined ? `<div style="font-size:12px;color:var(--text-dim);margin-top:2px;">置信度: ${(p.confidence * 100).toFixed(0)}%</div>` : ''}
        </div>
        <div style="display:flex;gap:6px;">
          <button onclick="editPattern('${p.id}')" style="padding:4px 8px;border:none;background:var(--accent);color:white;border-radius:6px;cursor:pointer;font-size:12px;">编辑</button>
          <button onclick="deletePattern('${p.id}')" style="padding:4px 8px;border:none;background:var(--negative);color:white;border-radius:6px;cursor:pointer;font-size:12px;">删除</button>
        </div>
      </div>
      ${p.summary ? `
        <div style="margin-bottom:8px;">
          <div style="font-size:11px;color:var(--text-dim);margin-bottom:4px;">规律描述</div>
          <div style="font-size:13px;">${escapeHtml(p.summary)}</div>
        </div>
      ` : ''}
      ${p.scenes && p.scenes.length > 0 ? `
        <div style="margin-bottom:8px;">
          <div style="font-size:11px;color:var(--text-dim);margin-bottom:4px;">适用场景</div>
          <div>${p.scenes.map(s => `<span style="background:#6A6A8B20;color:#6A6A8B;padding:2px 8px;border-radius:10px;font-size:12px;margin-right:4px;">${escapeHtml(s)}</span>`).join('')}</div>
        </div>
      ` : ''}
      ${p.tags && p.tags.length > 0 ? `
        <div style="margin-bottom:8px;">
          <div style="font-size:11px;color:var(--text-dim);margin-bottom:4px;">标签</div>
          <div>${p.tags.map(t => `<span style="background:var(--border);color:var(--text-dim);padding:2px 8px;border-radius:10px;font-size:12px;margin-right:4px;">${escapeHtml(t)}</span>`).join('')}</div>
        </div>
      ` : ''}
      ${p.source_events && p.source_events.length > 0 ? `
        <div>
          <div style="font-size:11px;color:var(--text-dim);margin-bottom:4px;">来源事件</div>
          <div style="font-size:11px;color:var(--text-dim);">${p.source_events.join(', ')}</div>
        </div>
      ` : ''}
      <div style="margin-top:12px;font-size:11px;color:var(--text-dim);">
        创建: ${new Date(p.created_at).toLocaleString()} | 更新: ${new Date(p.updated_at).toLocaleString()}
      </div>
    </div>
  `).join('');
}

async function loadPatterns() {
  const list = document.getElementById('pattern-list');
  try {
    let buckets = getCachedData('buckets');
    if (!buckets) {
      const resp = await authFetch('/api/buckets');
      if (!resp) return;
      const data = await resp.json();
      buckets = data.buckets || data;
      setCachedData('buckets', buckets);
    }
    const patterns = buckets.filter(m => m.type === 'pattern');
    renderPatterns(patterns);
  } catch(e) {
    list.innerHTML = `<p style="color:var(--negative)">加载失败: ${e.message}</p>`;
  }
}

function showIdentityEditor(id) {
  const modal = document.getElementById('identity-editor-modal');
  const title = document.getElementById('identity-editor-title');
  const nameInput = document.getElementById('identity-editor-name');
  const aliasesInput = document.getElementById('identity-editor-aliases');
  const traitsInput = document.getElementById('identity-editor-traits');
  const relationshipsInput = document.getElementById('identity-editor-relationships');
  const contentInput = document.getElementById('identity-editor-content');
  const idInput = document.getElementById('identity-editor-id');
  const basicInfoList = document.getElementById('identity-basic-info-list');
  
  const genderInput = document.getElementById('identity-editor-gender');
  const ageInput = document.getElementById('identity-editor-age');
  const occupationInput = document.getElementById('identity-editor-occupation');
  const interestsInput = document.getElementById('identity-editor-interests');
  
  if (id) {
    title.textContent = '编辑身份档案';
    authFetch('/api/bucket/' + id)
      .then(r => r.json())
      .then(data => {
        idInput.value = data.id;
        nameInput.value = data.name || data.topic || '';
        aliasesInput.value = data.aliases ? data.aliases.join(', ') : '';
        traitsInput.value = data.traits ? data.traits.join(', ') : '';
        relationshipsInput.value = data.relationships ? data.relationships.join('\n') : '';
        contentInput.value = data.content || '';
        
        genderInput.value = data.gender || '';
        ageInput.value = data.age || '';
        occupationInput.value = data.occupation || '';
        interestsInput.value = data.interests ? data.interests.join(', ') : '';
        
        basicInfoList.innerHTML = '';
        const info = data.basic_info || {};
        if (Object.keys(info).length === 0) {
          addBasicInfoRow();
        } else {
          Object.entries(info).forEach(([k, v]) => {
            basicInfoList.innerHTML += `
              <div style="display:flex;gap:8px;margin-bottom:6px;">
                <input type="text" value="${escapeHtml(k)}" style="flex:1;padding:8px;border-radius:8px;border:1px solid var(--border);" class="basic-info-key" />
                <input type="text" value="${escapeHtml(v)}" style="flex:1;padding:8px;border-radius:8px;border:1px solid var(--border);" class="basic-info-value" />
                <button onclick="addBasicInfoRow()" style="padding:8px 12px;border:none;background:var(--accent);color:white;border-radius:8px;cursor:pointer;">+</button>
              </div>
            `;
          });
        }
        loadIdentityRelatedList(id);
      });
  } else {
    title.textContent = '创建身份档案';
    idInput.value = '';
    nameInput.value = '';
    aliasesInput.value = '';
    traitsInput.value = '';
    relationshipsInput.value = '';
    contentInput.value = '';
    genderInput.value = '';
    ageInput.value = '';
    occupationInput.value = '';
    interestsInput.value = '';
    basicInfoList.innerHTML = '';
    addBasicInfoRow();
    loadIdentityRelatedList();
  }
  
  modal.style.display = 'flex';
}

function loadIdentityRelatedList(currentId) {
  const list = document.getElementById('identity-related-list');
  authFetch('/api/buckets')
    .then(r => r.json())
    .then(data => {
      const identities = (data.buckets || data).filter(b => b.type === 'identity' && b.id !== currentId);
      if (identities.length === 0) {
        list.innerHTML = '<div style="color:var(--text-dim);font-size:13px;text-align:center;padding:16px;">暂无其他名册</div>';
        return;
      }
      
      let currentRelations = [];
      if (currentId) {
        authFetch('/api/bucket/' + currentId)
          .then(r => r.json())
          .then(bucket => {
            currentRelations = bucket.related_buckets || [];
            renderRelatedList(identities, currentId, currentRelations);
          });
      } else {
        renderRelatedList(identities, currentId, []);
      }
      
      function renderRelatedList(items, cid, rels) {
        list.innerHTML = items.map(i => {
          const isRelated = rels.includes(i.id);
          return `
            <div style="display:flex;align-items:center;gap:10px;padding:6px 8px;border-radius:8px;cursor:pointer;transition:background 0.2s;" 
                 onclick="toggleIdentityRelation('${cid || ''}', '${i.id}')"
                 ${isRelated ? 'style="background:var(--accent);color:white;"' : 'onmouseover="this.style.background=var(--border)" onmouseout="this.style.background=\'\'"'}>
              <input type="checkbox" ${isRelated ? 'checked' : ''} style="margin:0;" />
              <span style="font-size:13px;">${escapeHtml(i.name || i.topic || '未命名')}</span>
            </div>
          `;
        }).join('');
      }
    });
}

async function toggleIdentityRelation(sourceId, targetId) {
  if (!sourceId) {
    alert('请先保存名册，再添加关联');
    return;
  }
  
  const list = document.getElementById('identity-related-list');
  const checkbox = list.querySelector(`input[onclick*="${targetId}"]`);
  const isChecked = checkbox.checked;
  
  try {
    const action = isChecked ? 'link' : 'unlink';
    const resp = await authFetch('/api/manage-relation', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action, bucket_id: sourceId, target_id: targetId})
    });
    const result = await resp.json();
    if (!result.success) {
      checkbox.checked = !isChecked;
      alert('操作失败: ' + result.message);
    }
  } catch (e) {
    checkbox.checked = !isChecked;
    alert('操作失败: ' + e.message);
  }
}

function closeIdentityEditor() {
  document.getElementById('identity-editor-modal').style.display = 'none';
}

function addBasicInfoRow() {
  const list = document.getElementById('identity-basic-info-list');
  list.innerHTML += `
    <div style="display:flex;gap:8px;margin-bottom:6px;">
      <input type="text" placeholder="键" style="flex:1;padding:8px;border-radius:8px;border:1px solid var(--border);" class="basic-info-key" />
      <input type="text" placeholder="值" style="flex:1;padding:8px;border-radius:8px;border:1px solid var(--border);" class="basic-info-value" />
      <button onclick="addBasicInfoRow()" style="padding:8px 12px;border:none;background:var(--accent);color:white;border-radius:8px;cursor:pointer;">+</button>
    </div>
  `;
}

async function saveIdentity() {
  const id = document.getElementById('identity-editor-id').value;
  const name = document.getElementById('identity-editor-name').value.trim();
  const aliases = document.getElementById('identity-editor-aliases').value.split(',').map(s => s.trim()).filter(s => s);
  const traits = document.getElementById('identity-editor-traits').value.split(',').map(s => s.trim()).filter(s => s);
  const relationships = document.getElementById('identity-editor-relationships').value.split('\n').map(s => s.trim()).filter(s => s);
  const content = document.getElementById('identity-editor-content').value;
  const msg = document.getElementById('identity-editor-msg');
  
  const gender = document.getElementById('identity-editor-gender').value;
  const age = document.getElementById('identity-editor-age').value;
  const occupation = document.getElementById('identity-editor-occupation').value.trim();
  const interests = document.getElementById('identity-editor-interests').value.split(',').map(s => s.trim()).filter(s => s);
  
  const keys = document.querySelectorAll('.basic-info-key');
  const values = document.querySelectorAll('.basic-info-value');
  const basicInfo = {};
  keys.forEach((k, i) => {
    const key = k.value.trim();
    const val = values[i].value.trim();
    if (key) basicInfo[key] = val;
  });
  
  if (!name) {
    msg.textContent = '请输入姓名';
    msg.style.color = 'var(--negative)';
    return;
  }
  
  const data = {
    name,
    type: 'identity',
    content: content || undefined,
    tags: [],
  };
  
  if (aliases.length > 0) data.aliases = aliases;
  if (traits.length > 0) data.traits = traits;
  if (Object.keys(basicInfo).length > 0) data.basic_info = basicInfo;
  if (relationships.length > 0) data.relationships = relationships;
  if (gender) data.gender = gender;
  if (age) data.age = parseInt(age);
  if (occupation) data.occupation = occupation;
  if (interests.length > 0) data.interests = interests;
  
  try {
    const url = id ? '/api/bucket/' + id : '/api/bucket';
    const method = id ? 'PUT' : 'POST';
    const resp = await authFetch(url, {
      method,
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data)
    });
    
    if (!resp) return;
    
    if (resp.ok) {
      msg.textContent = id ? '更新成功' : '创建成功';
      msg.style.color = 'var(--accent)';
      setTimeout(() => {
        closeIdentityEditor();
        loadIdentities();
      }, 500);
    } else {
      const text = await resp.text();
      msg.textContent = '保存失败: ' + text;
      msg.style.color = 'var(--negative)';
    }
  } catch(e) {
    msg.textContent = '保存失败: ' + e.message;
    msg.style.color = 'var(--negative)';
  }
}

async function deleteIdentity(id) {
  if (!confirm('确定删除这个身份档案吗？')) return;
  try {
    const resp = await authFetch('/api/bucket/' + id, { method: 'DELETE' });
    if (resp && resp.ok) {
      loadIdentities();
    }
  } catch(e) {
    alert('删除失败: ' + e.message);
  }
}

async function loadTimelines() {
  const result = document.getElementById('timeline-result');
  const empty = document.getElementById('timeline-empty');
  const loading = document.getElementById('timeline-loading');
  
  loading.style.display = 'none';
  result.style.display = 'none';
  
  try {
    const cached = getCachedData('timelines');
    if (cached && cached.length > 0) {
      const tl = cached[0];
      document.getElementById('timeline-title').textContent = tl.title;
      document.getElementById('timeline-summary').textContent = tl.summary || '暂无摘要';
      renderTimelinePhases(tl.phases || []);
      document.getElementById('timeline-phases').style.display = 'block';
      document.getElementById('timeline-all-arrow').style.transform = 'rotate(180deg)';
      result.style.display = 'block';
      return;
    }
    
    const resp = await authFetch('/api/timelines');
    if (!resp) return;
    const data = await resp.json();
    
    if (data.timelines && data.timelines.length > 0) {
      setCachedData('timelines', data.timelines);
      const tl = data.timelines[0];
      document.getElementById('timeline-title').textContent = tl.title;
      document.getElementById('timeline-summary').textContent = tl.summary || '暂无摘要';
      renderTimelinePhases(tl.phases || []);
      document.getElementById('timeline-phases').style.display = 'block';
      document.getElementById('timeline-all-arrow').style.transform = 'rotate(180deg)';
      result.style.display = 'block';
    } else {
      empty.style.display = 'block';
    }
  } catch(e) {
    empty.style.display = 'block';
  }
}

function renderTimelinePhases(phases) {
  const phasesHtml = phases.map((phase, index) => {
    const desc = phase.description || '';
    const shortDesc = desc.length > 100 ? desc.substring(0, 100) + '...' : desc;
    const hasDetails = (phase.key_points && phase.key_points.length > 0) || (phase.emotions && phase.emotions.length > 0) || desc.length > 100 || (phase.related_buckets && phase.related_buckets.length > 0);
    
    return `
    <div style="display:flex;gap:20px;margin-bottom:16px;position:relative;">
      <div style="flex-shrink:0;width:3px;background:var(--border);position:absolute;left:24px;top:0;height:100%;"></div>
      <div style="flex-shrink:0;width:50px;height:50px;border-radius:50%;background:linear-gradient(135deg,#6A6A8B,#4A7C59);display:flex;align-items:center;justify-content:center;color:white;font-size:16px;font-weight:bold;position:relative;z-index:1;">
        ${index + 1}
      </div>
      <div style="flex:1;">
        <div style="background:var(--surface);border-radius:16px;border:1px solid var(--border);overflow:hidden;">
          <div style="padding:16px;display:flex;justify-content:space-between;align-items:flex-start;cursor:pointer;" onclick="toggleTimelinePhase(${index})">
            <div style="flex:1;">
              ${phase.time ? `<div style="font-size:12px;color:var(--accent);font-weight:500;margin-bottom:6px;">${escapeHtml(phase.time)}</div>` : ''}
              <div style="font-size:14px;color:var(--text);line-height:1.5;">${escapeHtml(shortDesc)}</div>
              ${phase.related_buckets && phase.related_buckets.length > 0 ? `<div style="font-size:11px;color:var(--text-dim);margin-top:4px;">关联记忆: ${phase.related_buckets.length} 个</div>` : ''}
            </div>
            ${hasDetails ? `<div id="phase-arrow-${index}" style="flex-shrink:0;margin-left:12px;font-size:16px;color:var(--text-dim);transition:transform 0.2s;">▼</div>` : ''}
          </div>
          ${hasDetails ? `
            <div id="phase-details-${index}" style="display:none;border-top:1px solid var(--border);padding:0 16px 16px;">
              ${desc.length > 100 ? `
                <div style="margin-bottom:10px;padding-top:12px;">
                  <div style="font-size:11px;color:var(--text-dim);margin-bottom:4px;">完整描述</div>
                  <div style="font-size:13px;color:var(--text-secondary);line-height:1.6;">${escapeHtml(desc)}</div>
                </div>
              ` : ''}
              ${phase.key_points && phase.key_points.length > 0 ? `
                <div style="margin-bottom:10px;padding-top:12px;">
                  <div style="font-size:11px;color:var(--text-dim);margin-bottom:4px;">关键点</div>
                  <div>${phase.key_points.map(k => `<span style="background:#6A6A8B20;color:#6A6A8B;padding:3px 8px;border-radius:8px;font-size:12px;margin-right:6px;">${escapeHtml(k)}</span>`).join('')}</div>
                </div>
              ` : ''}
              ${phase.emotions && phase.emotions.length > 0 ? `
                <div style="margin-bottom:10px;padding-top:12px;">
                  <div style="font-size:11px;color:var(--text-dim);margin-bottom:4px;">情绪变化</div>
                  <div>${phase.emotions.map(e => `<span style="background:#FFB74D20;color:#FFB74D;padding:3px 8px;border-radius:8px;font-size:12px;margin-right:6px;">${escapeHtml(e)}</span>`).join('')}</div>
                </div>
              ` : ''}
              ${phase.related_buckets && phase.related_buckets.length > 0 ? `
                <div style="padding-top:12px;">
                  <div style="font-size:11px;color:var(--text-dim);margin-bottom:4px;">关联记忆桶</div>
                  <div style="display:flex;flex-wrap:wrap;gap:4px;">${phase.related_buckets.map(id => `<span onclick="loadBucketDetail('${id}')" style="cursor:pointer;background:#ECEFF1;color:#546E7A;padding:3px 8px;border-radius:8px;font-size:11px;">${id}</span>`).join('')}</div>
                </div>
              ` : ''}
            </div>
          ` : ''}
        </div>
      </div>
    </div>
    `;
  }).join('');
  
  document.getElementById('timeline-phases').innerHTML = phasesHtml;
}

function toggleTimelinePhase(index) {
  const details = document.getElementById(`phase-details-${index}`);
  const arrow = document.getElementById(`phase-arrow-${index}`);
  if (details && arrow) {
    if (details.style.display === 'none') {
      details.style.display = 'block';
      arrow.style.transform = 'rotate(180deg)';
    } else {
      details.style.display = 'none';
      arrow.style.transform = '';
    }
  }
}

function toggleTimelineAll() {
  const phases = document.getElementById('timeline-phases');
  const arrow = document.getElementById('timeline-all-arrow');
  
  if (phases.style.display === 'none') {
    phases.style.display = 'block';
    arrow.style.transform = 'rotate(180deg)';
  } else {
    phases.style.display = 'none';
    arrow.style.transform = '';
  }
}

async function generateTimeline() {
  const query = document.getElementById('timeline-query').value.trim();
  const loading = document.getElementById('timeline-loading');
  const result = document.getElementById('timeline-result');
  const empty = document.getElementById('timeline-empty');
  
  loading.style.display = 'block';
  result.style.display = 'none';
  empty.style.display = 'none';
  
  try {
    const resp = await authFetch('/api/timeline', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ query: query || "" })
    });
    
    if (!resp) return;
    
    const data = await resp.json();
    
    loading.style.display = 'none';
    
    if (data.error) {
      empty.innerHTML = `<div>${data.error}</div>`;
      empty.style.display = 'block';
      return;
    }
    
    document.getElementById('timeline-title').textContent = data.title;
    document.getElementById('timeline-summary').textContent = data.summary;
    
    const phasesHtml = data.phases.map((phase, index) => {
      const desc = phase.description || '';
      const shortDesc = desc.length > 100 ? desc.substring(0, 100) + '...' : desc;
      const hasDetails = (phase.key_points && phase.key_points.length > 0) || (phase.emotions && phase.emotions.length > 0) || desc.length > 100;
      
      return `
      <div style="display:flex;gap:20px;margin-bottom:16px;position:relative;">
        <div style="flex-shrink:0;width:3px;background:var(--border);position:absolute;left:24px;top:0;height:100%;"></div>
        <div style="flex-shrink:0;width:50px;height:50px;border-radius:50%;background:linear-gradient(135deg,#6A6A8B,#4A7C59);display:flex;align-items:center;justify-content:center;color:white;font-size:16px;font-weight:bold;position:relative;z-index:1;">
          ${index + 1}
        </div>
        <div style="flex:1;">
          <div style="background:var(--surface);border-radius:16px;border:1px solid var(--border);overflow:hidden;">
            <div style="padding:16px;display:flex;justify-content:space-between;align-items:flex-start;cursor:pointer;" onclick="toggleTimelinePhase(${index})">
              <div style="flex:1;">
                ${phase.time ? `<div style="font-size:12px;color:var(--accent);font-weight:500;margin-bottom:6px;">${escapeHtml(phase.time)}</div>` : ''}
                <div style="font-size:14px;color:var(--text);line-height:1.5;">${escapeHtml(shortDesc)}</div>
              </div>
              ${hasDetails ? `<div id="phase-arrow-${index}" style="flex-shrink:0;margin-left:12px;font-size:16px;color:var(--text-dim);transition:transform 0.2s;">▼</div>` : ''}
            </div>
            ${hasDetails ? `
              <div id="phase-details-${index}" style="display:none;border-top:1px solid var(--border);padding:0 16px 16px;">
                ${desc.length > 100 ? `
                  <div style="margin-bottom:10px;padding-top:12px;">
                    <div style="font-size:11px;color:var(--text-dim);margin-bottom:4px;">完整描述</div>
                    <div style="font-size:13px;color:var(--text-secondary);line-height:1.6;">${escapeHtml(desc)}</div>
                  </div>
                ` : ''}
                ${phase.key_points && phase.key_points.length > 0 ? `
                  <div style="margin-bottom:10px;padding-top:12px;">
                    <div style="font-size:11px;color:var(--text-dim);margin-bottom:4px;">关键点</div>
                    <div>${phase.key_points.map(k => `<span style="background:#6A6A8B20;color:#6A6A8B;padding:3px 8px;border-radius:8px;font-size:12px;margin-right:6px;">${escapeHtml(k)}</span>`).join('')}</div>
                  </div>
                ` : ''}
                ${phase.emotions && phase.emotions.length > 0 ? `
                  <div style="padding-top:12px;">
                    <div style="font-size:11px;color:var(--text-dim);margin-bottom:4px;">情绪变化</div>
                    <div>${phase.emotions.map(e => `<span style="background:#FFB74D20;color:#FFB74D;padding:3px 8px;border-radius:8px;font-size:12px;margin-right:6px;">${escapeHtml(e)}</span>`).join('')}</div>
                  </div>
                ` : ''}
              </div>
            ` : ''}
          </div>
        </div>
      </div>
      `;
    }).join('');
    
    document.getElementById('timeline-phases').innerHTML = phasesHtml;
    document.getElementById('timeline-phases').style.display = 'block';
    document.getElementById('timeline-all-arrow').style.transform = 'rotate(180deg)';
    result.style.display = 'block';
    
  } catch(e) {
    loading.style.display = 'none';
    empty.innerHTML = `<div style="font-size:48px;margin-bottom:16px;">❌</div><div>生成失败: ${e.message}</div>`;
    empty.style.display = 'block';
  }
}

// =============================================================
// Global AI Button / 全局AI按钮
// =============================================================
function toggleAIChat() {
  var chat = document.getElementById('global-ai-chat');
  var btn = document.getElementById('global-ai-btn');
  if (chat.style.display === 'none' || chat.style.display === '') {
    chat.style.display = 'flex';
    btn.style.transform = 'rotate(45deg)';
    setTimeout(() => document.getElementById('global-ai-input').focus(), 100);
  } else {
    chat.style.display = 'none';
    btn.style.transform = 'rotate(0deg)';
  }
}

async function sendGlobalAIChat() {
  var input = document.getElementById('global-ai-input');
  var messages = document.getElementById('global-ai-messages');
  var btn = document.getElementById('global-ai-send');
  var message = input.value.trim();
  
  if (!message) return;
  
  input.value = '';
  btn.disabled = true;
  btn.style.opacity = '0.6';
  
  messages.innerHTML += `
    <div style="display:flex;gap:10px;margin-bottom:12px;">
      <div style="width:30px;height:30px;border-radius:50%;background:var(--accent);color:white;display:flex;align-items:center;justify-content:center;font-size:12px;flex-shrink:0;">I</div>
      <div style="flex:1;background:white;border-radius:10px;padding:10px;border:1px solid var(--border);font-size:13px;">${escapeHtml(message)}</div>
    </div>
  `;
  messages.scrollTop = messages.scrollHeight;
  
  messages.innerHTML += `
    <div id="global-ai-loading" style="display:flex;gap:10px;margin-bottom:12px;">
      <div style="width:30px;height:30px;border-radius:50%;background:var(--positive);color:white;display:flex;align-items:center;justify-content:center;font-size:12px;flex-shrink:0;">🤖</div>
      <div style="flex:1;background:white;border-radius:10px;padding:10px;border:1px solid var(--border);color:var(--text-light);font-size:13px;">思考中...</div>
    </div>
  `;
  messages.scrollTop = messages.scrollHeight;
  
  try {
    var res = await fetch(BASE + '/api/ai-chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: message}),
    });
    var result = await res.json();
    
    document.getElementById('global-ai-loading').remove();
    
    if (result.ok) {
      messages.innerHTML += `
        <div style="display:flex;gap:10px;margin-bottom:12px;">
          <div style="width:30px;height:30px;border-radius:50%;background:var(--positive);color:white;display:flex;align-items:center;justify-content:center;font-size:12px;flex-shrink:0;">🤖</div>
          <div style="flex:1;background:white;border-radius:10px;padding:10px;border:1px solid var(--border);font-size:13px;">${escapeHtml(result.response)}</div>
        </div>
      `;
    } else {
      messages.innerHTML += `
        <div style="display:flex;gap:10px;margin-bottom:12px;">
          <div style="width:30px;height:30px;border-radius:50%;background:var(--negative);color:white;display:flex;align-items:center;justify-content:center;font-size:12px;flex-shrink:0;">🤖</div>
          <div style="flex:1;background:white;border-radius:10px;padding:10px;border:1px solid var(--border);color:var(--negative);font-size:13px;">${escapeHtml(result.error || 'AI回答失败')}</div>
        </div>
      `;
    }
  } catch (e) {
    document.getElementById('global-ai-loading').remove();
    messages.innerHTML += `
      <div style="display:flex;gap:10px;margin-bottom:12px;">
        <div style="width:30px;height:30px;border-radius:50%;background:var(--negative);color:white;display:flex;align-items:center;justify-content:center;font-size:12px;flex-shrink:0;">🤖</div>
        <div style="flex:1;background:white;border-radius:10px;padding:10px;border:1px solid var(--border);color:var(--negative);font-size:13px;">网络错误: ${escapeHtml(e.message)}</div>
      </div>
    `;
  } finally {
    btn.disabled = false;
    btn.style.opacity = '1';
    messages.scrollTop = messages.scrollHeight;
  }
}

// ========================================
// Dark Mode Toggle / 深色模式切换
// ========================================
function toggleTheme() {
  const body = document.body;
  body.classList.toggle('dark');
  const isDark = body.classList.contains('dark');
  localStorage.setItem('ombre-brain-theme', isDark ? 'dark' : 'light');
}

// Load saved theme on page load
(function loadTheme() {
  const saved = localStorage.getItem('ombre-brain-theme');
  if (saved === 'dark') {
    document.body.classList.add('dark');
  }
})();

// ========================================
// Enhanced Search / 增强搜索功能
// ========================================
let searchDebounceTimer = null;

// Enhanced search that shows results in the inline panel
function performSearch(query) {
  const panel = document.getElementById('search-results-panel');
  if (!panel) return;
  
  if (!query || query.length < 1) {
    panel.classList.remove('open');
    return;
  }
  
  panel.innerHTML = '<div class="search-loading">搜索中…</div>';
  panel.classList.add('open');
  
  fetch(BASE + '/api/search?q=' + encodeURIComponent(query), { credentials: 'include' })
    .then(function(resp) {
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      return resp.json();
    })
    .then(function(data) {
      var results = data.results || data;
      if (!Array.isArray(results) || results.length === 0) {
        panel.innerHTML = '<div class="search-no-results">未找到匹配的记忆</div>';
        return;
      }
      
      var html = '';
      var maxResults = Math.min(results.length, 10);
      for (var i = 0; i < maxResults; i++) {
        var r = results[i];
        var name = esc(r.name || r.topic || '未命名');
        var type = r.type || 'event';
        var score = r.importance || r.score || 0;
        
        html += '<div class="search-result-item" onclick="showDetail(\'' + r.id + '\')">' +
          '<span class="sr-name">' + name + '</span>' +
          '<span class="sr-type">' + type + '</span>' +
          '<span class="sr-score">' + (typeof score === 'number' ? score.toFixed(1) : score) + '</span>' +
        '</div>';
      }
      
      if (results.length > 10) {
        html += '<div class="search-no-results" style="padding:8px;font-size:11px;">还有 ' + (results.length - 10) + ' 条结果…</div>';
      }
      
      panel.innerHTML = html;
    })
    .catch(function(e) {
      panel.innerHTML = '<div class="search-no-results">搜索失败: ' + e.message + '</div>';
    });
}

// Override the existing search input listener to use inline panel
document.getElementById('search-input').addEventListener('input', function(e) {
  clearTimeout(searchDebounceTimer);
  var q = e.target.value.trim();
  searchDebounceTimer = setTimeout(function() {
    if (q) {
      performSearch(q);
    } else {
      var panel = document.getElementById('search-results-panel');
      if (panel) panel.classList.remove('open');
    }
  }, 300);
});

document.getElementById('search-input').addEventListener('blur', function() {
  setTimeout(function() {
    var panel = document.getElementById('search-results-panel');
    if (panel) panel.classList.remove('open');
  }, 200);
});

document.getElementById('search-input').addEventListener('focus', function() {
  var q = this.value.trim();
  if (q) {
    var panel = document.getElementById('search-results-panel');
    if (panel) panel.classList.add('open');
  }
});

// ========================================
// Enhanced Stats Cards / 增强统计卡片
// ========================================
function renderStatsCards() {
  var container = document.getElementById('stats-cards');
  if (!container) return;
  
  var total = allBuckets.length;
  var pinned = allBuckets.filter(function(b) { return b.pinned; }).length;
  var feels = allBuckets.filter(function(b) { return b.type === 'feel'; }).length;
  var identities = allBuckets.filter(function(b) { return b.type === 'identity'; }).length;
  var patterns = allBuckets.filter(function(b) { return b.type === 'pattern'; }).length;
  var events = allBuckets.filter(function(b) { return !b.type || b.type === 'event'; }).length;
  var resolved = allBuckets.filter(function(b) { return b.resolved; }).length;
  
  var cards = [
    { icon: '🧠', label: '总记忆', value: total, color: 'var(--accent)' },
    { icon: '👤', label: '身份', value: identities, color: '#4A7C59' },
    { icon: '🔮', label: '模式', value: patterns, color: '#6A6A8B' },
    { icon: '📌', label: '事件', value: events, color: '#2F4F4F' },
    { icon: '📌', label: '钉选', value: pinned, color: '#9A7B4F' },
    { icon: '💭', label: '感受', value: feels, color: '#8B6A6A' },
    { icon: '✓', label: '已解决', value: resolved, color: '#81C784' },
  ];
  
  container.innerHTML = cards.map(function(c) {
    return '<div class="stat-card" style="border-top:3px solid ' + c.color + ';">' +
      '<span class="stat-icon">' + c.icon + '</span>' +
      '<div class="stat-value" style="color:' + c.color + ';">' + c.value + '</div>' +
      '<div class="stat-label">' + c.label + '</div>' +
    '</div>';
  }).join('');
}

// ========================================
// Mood/Emotion Summary Widget / 情绪总览组件
// ========================================
function renderMoodWidget() {
  var widget = document.getElementById('mood-widget');
  var bars = document.getElementById('mood-bars');
  var summary = document.getElementById('mood-summary');
  var dateEl = document.getElementById('mood-date');
  
  if (!widget || !bars) return;
  
  var emotionCounts = {};
  allBuckets.forEach(function(b) {
    if (b.emotions && Array.isArray(b.emotions)) {
      b.emotions.forEach(function(e) {
        var label = e.label || e;
        emotionCounts[label] = (emotionCounts[label] || 0) + 1;
      });
    }
  });
  
  var entries = Object.entries(emotionCounts);
  if (entries.length === 0) {
    widget.style.display = 'none';
    return;
  }
  
  widget.style.display = 'block';
  
  var sorted = entries.sort(function(a, b) { return b[1] - a[1]; });
  var topEmotions = sorted.slice(0, 6);
  var maxCount = topEmotions.length > 0 ? topEmotions[0][1] : 1;
  
  var emotionColorMap = {
    '喜悦': '#FFB74D', '快乐': '#FFB74D', '幸福': '#FFB74D', '满意': '#FFB74D',
    '悲伤': '#9C27B0', '忧伤': '#9C27B0', '悲痛': '#9C27B0', '绝望': '#9C27B0',
    '愤怒': '#F44336', '生气': '#F44336', '暴怒': '#F44336', '烦恼': '#F44336',
    '恐惧': '#E91E63', '害怕': '#E91E63', '焦虑': '#E91E63', '不安': '#E91E63',
    '惊讶': '#00BCD4', '震惊': '#00BCD4', '好奇': '#00BCD4', '惊愕': '#00BCD4',
    '信任': '#4CAF50', '热爱': '#4CAF50', '接受': '#4CAF50', '迷恋': '#4CAF50',
    '期待': '#FFC107', '希望': '#FFC107', '兴奋': '#FFC107', '狂喜': '#FFC107',
    '厌恶': '#795548', '反感': '#795548', '憎恨': '#795548', '不悦': '#795548',
  };
  
  var barsHtml = '';
  topEmotions.forEach(function(entry) {
    var label = entry[0];
    var count = entry[1];
    var pct = Math.round((count / maxCount) * 100);
    var color = emotionColorMap[label] || '#6A6A8B';
    
    barsHtml += '<div class="mood-bar-row">' +
      '<span class="mood-bar-label">' + esc(label) + '</span>' +
      '<div class="mood-bar-track">' +
        '<div class="mood-bar-fill" style="width:' + pct + '%;background:' + color + ';"></div>' +
      '</div>' +
      '<span class="mood-bar-count">' + count + '</span>' +
    '</div>';
  });
  bars.innerHTML = barsHtml;
  
  var totalEmotions = entries.reduce(function(acc, e) { return acc + e[1]; }, 0);
  var dominant = sorted[0] ? sorted[0][0] : '—';
  
  var positiveLabels = ['喜悦', '快乐', '幸福', '满意', '信任', '热爱', '期待', '希望', '兴奋'];
  var negativeLabels = ['悲伤', '愤怒', '恐惧', '厌恶', '绝望', '焦虑', '不安'];
  var posCount = 0, negCount = 0;
  entries.forEach(function(e) {
    if (positiveLabels.includes(e[0])) posCount += e[1];
    else if (negativeLabels.includes(e[0])) negCount += e[1];
  });
  
  var summaryHtml = '';
  summaryHtml += '<div class="mood-summary-item"><span class="mood-summary-dot" style="background:' + (emotionColorMap[dominant] || '#6A6A8B') + ';"></span>主导情绪: <strong>' + esc(dominant) + '</strong></div>';
  summaryHtml += '<div class="mood-summary-item">📊 总标记: ' + totalEmotions + '</div>';
  if (posCount > 0 || negCount > 0) {
    var ratio = posCount + negCount > 0 ? Math.round((posCount / (posCount + negCount)) * 100) : 50;
    summaryHtml += '<div class="mood-summary-item">😊 正向: ' + ratio + '%</div>';
  }
  summaryHtml += '<div class="mood-summary-item">📅 ' + new Date().toLocaleDateString('zh-CN') + '</div>';
  summary.innerHTML = summaryHtml;
}

// ========================================
// Memory Timeline / 记忆时间线
// ========================================
function renderMemoryTimeline() {
  var container = document.getElementById('timeline-container');
  var countEl = document.getElementById('timeline-count');
  var timelineEl = document.getElementById('memory-timeline');
  
  if (!container || !timelineEl) return;
  
  if (!allBuckets || allBuckets.length === 0) {
    timelineEl.style.display = 'none';
    return;
  }
  
  // Sort buckets by created time (newest first), filter out those without timestamps
  var withTime = allBuckets.filter(function(b) {
    return b.created || b.last_active || b.metadata?.created || b.metadata?.last_active;
  });
  
  if (withTime.length === 0) {
    timelineEl.style.display = 'none';
    return;
  }
  
  var sorted = withTime.sort(function(a, b) {
    var ta = a.created || a.last_active || (a.metadata && (a.metadata.created || a.metadata.last_active)) || '';
    var tb = b.created || b.last_active || (b.metadata && (b.metadata.created || b.metadata.last_active)) || '';
    return tb.localeCompare(ta);
  });
  
  var recent = sorted.slice(0, 5);
  timelineEl.style.display = 'block';
  
  if (countEl) countEl.textContent = '最近 ' + recent.length + ' 条';
  
  var html = '';
  recent.forEach(function(b) {
    var timeStr = b.created || b.last_active || (b.metadata && (b.metadata.created || b.metadata.last_active)) || '';
    var name = esc(b.name || b.topic || '未命名');
    var preview = esc((b.content || '').substring(0, 80));
    var tags = b.tags || (b.metadata && b.metadata.tags) || [];
    var tagsHtml = '';
    if (tags.length > 0) {
      tagsHtml = '<div class="tl-tags">' + tags.slice(0, 3).map(function(t) {
        return '<span class="tl-tag">' + esc(t) + '</span>';
      }).join('') + '</div>';
    }
    
    html += '<div class="timeline-entry" onclick="showDetail(\'' + b.id + '\')">' +
      '<div class="tl-time">' + formatTimeAgo(timeStr) + '</div>' +
      '<div class="tl-name">' + name + '</div>' +
      (preview ? '<div class="tl-preview">' + preview + '…</div>' : '') +
      tagsHtml +
    '</div>';
  });
  
  container.innerHTML = html;
}

// ========================================
// Override updateStats to also render new widgets / 增强updateStats
// ========================================
var originalUpdateStats = updateStats;
updateStats = function() {
  originalUpdateStats();
  renderStatsCards();
  renderMoodWidget();
  renderMemoryTimeline();
};

