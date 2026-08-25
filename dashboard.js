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

function hexToRgb(hex) {
  var result = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
  return result ? parseInt(result[1], 16) + ',' + parseInt(result[2], 16) + ',' + parseInt(result[3], 16) : '0,0,0';
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
  options = options || {};
  options.credentials = 'include';
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
var selectedBuckets = new Set();

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

function buildTagDisplay(tags, primaryTags, subTags) {
  if ((!tags || tags.length === 0) && (!primaryTags || primaryTags.length === 0)) return '—';

  // --- 优先使用主/副标签字段；旧数据回退到泛化/具体划分 ---
  // --- prefer primary/sub tags; fall back to generic/specific split ---
  var primary = primaryTags && primaryTags.length > 0 ? primaryTags : tags.filter(t => GENERIC_TAGS.includes(t));
  var sub = subTags && subTags.length > 0 ? subTags : tags.filter(t => !GENERIC_TAGS.includes(t));

  var html = '';
  if (primary.length > 0) {
    html += '<div style="margin-bottom:4px;">';
    html += '<span style="font-size:10px;color:var(--text-light);margin-right:4px;">主:</span>';
    html += primary.map(t => '<span style="background:var(--accent);color:white;padding:2px 8px;border-radius:10px;font-size:11px;margin-right:4px;">' + esc(t) + '</span>').join('');
    html += '</div>';
  }
  if (sub.length > 0) {
    html += '<div>';
    html += '<span style="font-size:10px;color:var(--text-light);margin-right:4px;">副:</span>';
    html += sub.map(t => '<span style="background:var(--border);color:var(--text);padding:2px 8px;border-radius:10px;font-size:11px;margin-right:4px;">' + esc(t) + '</span>').join('');
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
    document.getElementById('experience-view').style.display = target === 'experience' ? '' : 'none';
    document.getElementById('anchor-view').style.display = target === 'anchor' ? '' : 'none';
    document.getElementById('identity-view').style.display = target === 'identity' ? '' : 'none';
    document.getElementById('timeline-view').style.display = target === 'timeline' ? '' : 'none';
    document.getElementById('candlestick-view').style.display = target === 'candlestick' ? '' : 'none';
    document.getElementById('cycle-view').style.display = target === 'cycle' ? '' : 'none';
    document.getElementById('journal-view').style.display = target === 'journal' ? '' : 'none';
    document.getElementById('network-view').style.display = target === 'network' ? '' : 'none';
    document.getElementById('config-view').style.display = target === 'config' ? '' : 'none';
    if (target === 'network') loadNetwork();
    if (target === 'config') loadConfig();
    if (target === 'identity') loadIdentities();
    if (target === 'experience') loadExperiences();
    // --- 锚点自动触发：进入页面即扫描高情绪记忆生成锚点，无需手动点击 ---
    // --- anchors trigger automatically on entering the tab, no manual click ---
    if (target === 'anchor') { loadAnchors(); autoCreateAnchors(); }
    if (target === 'timeline') loadTimelines();
    if (target === 'candlestick') loadCandlesticks();
    if (target === 'cycle') loadCycle();
    if (target === 'journal') loadJournal();
  });
});

// --- 导航标签切换 ---
// --- tab switching ---

async function loadBuckets() {
  try {
    showLoading('bucket-list');
    var timeRangeEl = document.getElementById('timeRange');
    var days = timeRangeEl ? timeRangeEl.value : '0';

    if (days === '0') {
      const cached = getCachedData('buckets');
      if (cached) {
        allBuckets = cached;
        updateStats();
        buildFilters();
        renderBuckets(allBuckets);
        return;
      }
    }

    var url = BASE + '/api/buckets';
    if (days && days !== '0') {
      url += '?days=' + encodeURIComponent(days);
    }

    const res = await fetch(url);
    const data = await res.json();
    if (!res.ok) {
      throw new Error((data && data.error) ? data.error : `HTTP ${res.status}`);
    }
    const buckets = data.buckets || data;
    allBuckets = buckets;
    if (days === '0') {
      setCachedData('buckets', buckets);
    }
    updateStats();
    buildFilters();
    renderBuckets(allBuckets);
  } catch (e) {
    showError('bucket-list', e.message);
  }
}

async function loadExpiringMemories() {
  var card = document.getElementById('expiring-memories');
  var listEl = document.getElementById('expiring-list');
  var countEl = document.getElementById('expiring-count');
  if (!card || !listEl) return;

  try {
    var res = await authFetch(BASE + '/api/buckets?expiring=1&limit=1000');
    if (!res) return;
    var data = await res.json();
    var buckets = data.buckets || data;
    var expiring = buckets.filter(function(b) { return (b.score || 0) < 0.3; });

    card.style.display = 'block';

    if (!expiring.length) {
      listEl.innerHTML = '<div style="font-size:13px;color:var(--text-dim);padding:8px 0;">暂无</div>';
      if (countEl) countEl.textContent = '';
      return;
    }

    if (countEl) countEl.textContent = expiring.length + ' 项';

    var html = '';
    for (var i = 0; i < expiring.length; i++) {
      var b = expiring[i];
      var shortId = b.id.substring(0, 8);
      html += '<div style="display:flex;align-items:center;gap:10px;padding:8px 12px;background:var(--card-bg);border-radius:var(--radius-sm);border:1px solid var(--border);margin-bottom:6px;cursor:pointer;" onclick="showDetail(\'' + b.id + '\')">' +
        '<div style="flex:1;min-width:0;">' +
          '<div style="font-size:13px;font-weight:500;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + esc(b.name) + '</div>' +
          '<div style="font-size:11px;color:var(--text-light);">得分 ' + (b.score || 0).toFixed(2) + ' · #' + shortId + '</div>' +
        '</div>' +
        '<span onclick="event.stopPropagation();" class="row-actions">' +
          '<button class="row-action-btn" onclick="editBucket(\'' + b.id + '\')" title="编辑">编辑</button>' +
          '<button class="row-action-btn danger" onclick="deleteBucket(\'' + b.id + '\')" title="删除">删除</button>' +
          '<button class="row-action-btn keep" onclick="keepMemory(\'' + b.id + '\')" title="保留">保留</button>' +
        '</span>' +
      '</div>';
    }
    listEl.innerHTML = html;
  } catch(e) {
    console.error('loadExpiringMemories failed:', e);
  }
}

async function keepMemory(bucketId) {
  try {
    var resp = await authFetch(BASE + '/api/bucket/' + bucketId, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({importance: 8})
    });
    if (resp && resp.ok) {
      invalidateCache('buckets');
      loadExpiringMemories();
      loadBuckets();
    } else {
      alert('保留失败');
    }
  } catch(e) {
    alert('保留失败: ' + e.message);
  }
}

function updateStats() {
  const total = allBuckets.length;
  const pinned = allBuckets.filter(b => b.pinned).length;
  const feels = allBuckets.filter(b => b.type === 'feel').length;
  const identities = allBuckets.filter(b => b.type === 'identity').length;
  const patterns = allBuckets.filter(b => b.type === 'pattern').length;
  const events = allBuckets.filter(b => !b.type || b.type === 'event').length;
  
  var statsHtml = '<span style="font-weight:600;color:var(--text);font-size:13px;">' + total + ' 条记忆</span>';
  var statParts = [
    ['身份', identities, '#4A7C59'],
    ['模式', patterns, '#6A6A8B'],
    ['事件', events, '#2F4F4F'],
    ['感受', feels, '#8B6A6A'],
    ['钉选', pinned, '#9A7B4F']
  ];
  statsHtml += statParts.map(function(p) {
    return '<span style="display:inline-flex;align-items:center;gap:5px;">' +
      '<span style="width:7px;height:7px;border-radius:50%;background:' + p[2] + ';opacity:0.75;"></span>' +
      p[0] + ' ' + p[1] + '</span>';
  }).join('');
  document.getElementById('stats').innerHTML = statsHtml;
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
        emotionDisplay = '<span style="font-size:11px;color:var(--text-light);">未标注</span>';
      }
      
      var shortId = b.id.substring(0, 8);
      var preview = esc((b.content_preview || b.content || '').replace(/\n/g, ' ').substring(0, 150));
      var typeColors = {
        'identity': '#4A7C59', 'pattern': '#9A7B4F', 'feel': '#8B4A4A',
        'event': '#2F4F4F', 'experience': '#6A6A8B', 'candlestick': '#DAA520'
      };
      var typeColor = typeColors[bucketType] || '#888';
      var lockBadge = b.is_private ? '<span style="display:inline-flex;align-items:center;gap:2px;padding:1px 6px;border-radius:4px;background:rgba(220,120,120,0.12);color:#C0392B;font-size:10px;font-weight:500;">隐私</span>' : '';
      var checkedAttr = selectedBuckets.has(b.id) ? 'checked' : '';
      var checkboxHtml = '<input type="checkbox" class="bucket-checkbox" ' + checkedAttr + ' onclick="event.stopPropagation();toggleBucketSelection(\'' + b.id + '\')" style="margin-right:8px;">';

      html += '<div class="bucket-row' + (b.type === 'identity' ? ' identity-card' : b.type === 'pattern' ? ' pattern-card' : '') + '" data-bucket-id="' + b.id + '">' +
        '<div class="name">' + checkboxHtml + lockBadge + esc(b.name) + '<span style="color:var(--text-light);font-size:11px;margin-left:6px;font-weight:400;">#' + shortId + '</span>' +
          '<span class="row-actions">' +
            '<button class="row-action-btn" onclick="event.stopPropagation();editBucket(\'' + b.id + '\')" title="编辑">编辑</button>' +
            '<button class="row-action-btn danger" onclick="event.stopPropagation();deleteBucket(\'' + b.id + '\')" title="删除">删除</button>' +
          '</span>' +
        '</div>' +
        (preview ? '<div class="preview">' + preview + '</div>' : '') +
        '<div class="row-tags">' +
          '<span style="padding:1px 6px;border-radius:4px;background:rgba(' + hexToRgb(typeColor) + ',0.1);color:' + typeColor + ';" class="type">' + bucketType + '</span>' +
          (b.domain && b.domain.length ? '<span class="domain">' + b.domain.join(', ') + '</span>' : '') +
          '<span class="emotion">' + emotionDisplay + '</span>' +
          (b.pinned ? '<span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:#9A7B4F;margin-right:4px;"></span>' : '') +
          '<span style="margin-left:auto;color:var(--text-light);font-size:11px;">' + (b.score || 0).toFixed(2) + '</span>' +
        '</div>' +
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
    var res = await fetch(BASE + '/api/search?q=' + encodeURIComponent(query), { credentials: 'include' });
    var results = await res.json();
    renderBuckets(results);
  } catch (e) {
    console.error('Search failed:', e);
  }
}

async function showDetail(id, prefetched) {
  var panel = document.getElementById('detail-panel');
  var content = document.getElementById('detail-content');
  content.innerHTML = '<div class="loading">加载中…</div>';
  panel.classList.add('open');

  try {
    var b = prefetched;
    if (!b) {
      var res = await fetch(BASE + '/api/bucket/' + id, { credentials: 'include' });
      b = await res.json();
    }
    var meta = b.metadata || {};
    var bucketType = meta.type || 'event';
    var _contentShown = false;

    // --- Handle locked private buckets ---
    // --- 处理隐私锁定的记忆桶 ---
    if (b.locked) {
      var detailHtml = '<h2>' + esc(meta.name || id) + '</h2>';
      detailHtml += '<div style="text-align:center;padding:40px 20px;">';
      detailHtml += '<div style="font-size:14px;color:var(--text-dim);margin-bottom:20px;">这是一条隐私记忆，请输入密码查看完整内容</div>';
      detailHtml += '<input type="password" id="privacy-unlock-input" placeholder="输入密码" style="width:200px;padding:10px 14px;border-radius:12px;border:1px solid var(--border);background:var(--surface);color:var(--text);font-family:inherit;text-align:center;margin-bottom:12px;" />';
      detailHtml += '<div id="privacy-unlock-error" style="color:var(--negative);font-size:12px;margin-bottom:12px;min-height:16px;"></div>';
      detailHtml += '<button onclick="unlockPrivacyBucket(\'' + id + '\')" style="padding:10px 24px;border:none;background:var(--accent);color:white;border-radius:12px;cursor:pointer;font-size:13px;">查看内容</button>';
      detailHtml += '</div>';
      content.innerHTML = detailHtml;

      var unlockInput = document.getElementById('privacy-unlock-input');
      unlockInput.focus();
      unlockInput.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') unlockPrivacyBucket(id);
      });
      return;
    }
    
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
      // --- 详情固定顺序：标题 → 主/副标签 → 正文 → 元数据（时间/权重/情绪） ---
      // --- fixed order: title → primary/sub tags → body → metadata ---
      _contentShown = true;
      detailHtml += '<div class="detail-tags">' + buildTagDisplay(meta.tags || [], meta.primary_tags || [], meta.sub_tags || []) + '</div>';
      detailHtml += '<div class="detail-content">' + esc(b.content) + '</div>';
      detailHtml += '<div class="detail-meta">' +
        '<div class="field"><label>ID</label>' + id + '</div>' +
        '<div class="field"><label>类型</label>' + bucketType + '</div>' +
        '<div class="field"><label>域</label>' + (meta.domain || []).join(', ') + '</div>';
      
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
    
    if (!_contentShown) {
      detailHtml += '<div class="detail-content">' + esc(b.content) + '</div>';
    }
    
    detailHtml += '<div style="margin-top:24px;padding-top:20px;border-top:1px solid var(--border);display:flex;gap:12px;">';
    // 编辑：类型专用编辑器优先，其余走通用编辑弹窗
    if (bucketType === 'identity') {
      detailHtml += '<button class="detail-action-btn" onclick="showIdentityEditor(\'' + id + '\')">编辑</button>';
    } else if (bucketType === 'pattern') {
      detailHtml += '<button class="detail-action-btn" onclick="editPattern(\'' + id + '\')">编辑</button>';
    } else {
      detailHtml += '<button class="detail-action-btn" onclick="editBucket(\'' + id + '\')">编辑</button>';
    }
    detailHtml += '<button class="detail-action-btn danger" onclick="deleteBucket(\'' + id + '\')">删除</button>';
    detailHtml += '<button class="detail-action-btn primary" onclick="showAddRelationModal(\'' + id + '\')">+ 添加关联记忆</button>';
    if (meta.is_private) {
      detailHtml += '<button class="detail-action-btn" onclick="togglePrivacyLock(\'' + id + '\', false)">解除锁定</button>';
    } else {
      detailHtml += '<button class="detail-action-btn" onclick="showPrivacyLockDialog(\'' + id + '\')">设为隐私</button>';
    }
    detailHtml += '</div>';
    
    content.innerHTML = detailHtml;
  } catch (e) {
    content.innerHTML = '<div class="loading">加载失败: ' + e.message + '</div>';
  }
}

// ========================================
// 通用记忆编辑 / 删除：所有入口可对任意记忆进行增删改查
// ========================================

// 兼容旧入口：详情面板/时间链中的关联记忆等点击跳转查看
function loadBucketDetail(id) { showDetail(id); }

// 打开通用编辑弹窗（event/feel/dynamic/permanent 等普通桶）
async function editBucket(id) {
  try {
    const resp = await authFetch('/api/bucket/' + id);
    if (!resp) return;
    const b = await resp.json();
    openBucketEditor(b);
  } catch(e) {
    alert('加载失败: ' + e.message);
  }
}

function openBucketEditor(b) {
  const meta = b.metadata || {};
  document.getElementById('bucket-editor-id').value = b.id;
  document.getElementById('bucket-editor-type').value = meta.type || 'event';
  document.getElementById('bucket-editor-title').textContent = '编辑记忆';
  document.getElementById('bucket-editor-name').value = meta.name || b.name || '';
  document.getElementById('bucket-editor-type-display').value = meta.type || 'event';
  document.getElementById('bucket-editor-content').value = b.content || '';
  document.getElementById('bucket-editor-tags').value = (meta.tags || []).join(', ');
  document.getElementById('bucket-editor-domain').value = (meta.domain || []).join(', ');
  document.getElementById('bucket-editor-importance').value = meta.importance || 5;
  const emotions = (meta.emotions || []).map(e => e.label).filter(Boolean);
  document.getElementById('bucket-editor-emotions').value = emotions.join(', ');
  const valence = (meta.valence != null && !isNaN(meta.valence)) ? meta.valence : 0.5;
  const arousal = (meta.arousal != null && !isNaN(meta.arousal)) ? meta.arousal : 0.3;
  document.getElementById('bucket-editor-valence').value = valence;
  document.getElementById('bucket-editor-valence-value').textContent = valence.toFixed(2);
  document.getElementById('bucket-editor-arousal').value = arousal;
  document.getElementById('bucket-editor-arousal-value').textContent = arousal.toFixed(2);
  document.getElementById('bucket-editor-msg').textContent = '';
  document.getElementById('bucket-editor-modal').style.display = 'flex';
}

function closeBucketEditor() {
  document.getElementById('bucket-editor-modal').style.display = 'none';
}

async function saveBucketEdit() {
  const id = document.getElementById('bucket-editor-id').value;
  const msg = document.getElementById('bucket-editor-msg');
  if (!id) { closeBucketEditor(); return; }

  const valence = parseFloat(document.getElementById('bucket-editor-valence').value);
  const arousal = parseFloat(document.getElementById('bucket-editor-arousal').value);
  const emotionLabels = document.getElementById('bucket-editor-emotions').value.split(',').map(s => s.trim()).filter(s => s);
  const emotions = emotionLabels.map(label => ({
    label: label,
    intensity: Math.max(0.3, Math.abs(valence - 0.5) * 2),
    polarity: valence > 0.55 ? 'positive' : (valence < 0.45 ? 'negative' : 'neutral'),
    arousal_level: arousal > 0.66 ? 'high' : (arousal > 0.33 ? 'medium' : 'low'),
    duration: 'short'
  }));

  const data = {
    name: document.getElementById('bucket-editor-name').value.trim(),
    content: document.getElementById('bucket-editor-content').value,
    tags: document.getElementById('bucket-editor-tags').value.split(',').map(s => s.trim()).filter(s => s),
    domain: document.getElementById('bucket-editor-domain').value.split(',').map(s => s.trim()).filter(s => s),
    importance: parseInt(document.getElementById('bucket-editor-importance').value) || 5,
    emotions: emotions,
    valence: valence,
    arousal: arousal
  };
  if (emotions.length > 0) data.dominant_emotion = emotions[0].label;

  try {
    const resp = await authFetch('/api/bucket/' + id, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data)
    });
    if (!resp) return;
    if (resp.ok) {
      msg.textContent = '保存成功';
      msg.style.color = 'var(--accent)';
      setTimeout(() => {
        closeBucketEditor();
        invalidateCache('buckets');
        loadBuckets();
        loadExpiringMemories();
        showDetail(id);
      }, 400);
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

// 单条删除任意记忆桶（移入回收站，24h 内可恢复）
async function deleteBucket(id) {
  if (!confirm('确定要删除这条记忆吗？删除后进入回收站，24 小时后自动清理。')) return;
  try {
    const resp = await authFetch('/api/bucket/' + id, { method: 'DELETE' });
    if (!resp) return;
    if (resp.ok) {
      alert('已删除');
      invalidateCache('buckets');
      loadBuckets();
      loadExpiringMemories();
      const panel = document.getElementById('detail-panel');
      if (panel) panel.classList.remove('open');
    } else {
      const data = await resp.json().catch(() => ({}));
      alert('删除失败: ' + (data.error || '未知错误'));
    }
  } catch(e) {
    alert('删除失败: ' + e.message);
  }
}

// 滑块实时显示
document.addEventListener('DOMContentLoaded', function() {
  var vEl = document.getElementById('bucket-editor-valence');
  var aEl = document.getElementById('bucket-editor-arousal');
  if (vEl) vEl.addEventListener('input', function() {
    document.getElementById('bucket-editor-valence-value').textContent = parseFloat(this.value).toFixed(2);
  });
  if (aEl) aEl.addEventListener('input', function() {
    document.getElementById('bucket-editor-arousal-value').textContent = parseFloat(this.value).toFixed(2);
  });
});

async function unlockPrivacyBucket(id) {
  var input = document.getElementById('privacy-unlock-input');
  var errorEl = document.getElementById('privacy-unlock-error');
  if (!input) return;

  var password = input.value;
  if (!password) {
    errorEl.textContent = '请输入密码';
    return;
  }

  try {
    var res = await fetch(BASE + '/api/bucket/' + id + '?password=' + encodeURIComponent(password), { credentials: 'include' });
    var b = await res.json();
    if (b.locked) {
      errorEl.textContent = '密码错误';
      input.value = '';
      input.focus();
      return;
    }
    // Password correct — re-render detail with full content
    showDetail(id, b);
  } catch (e) {
    errorEl.textContent = '解锁失败: ' + e.message;
  }
}

function showPrivacyLockDialog(bucketId) {
  var modal = document.createElement('div');
  modal.className = 'modal-overlay';
  modal.style.display = 'flex';
  modal.innerHTML = `
    <div style="background:var(--surface-solid);border-radius:24px;padding:24px;width:400px;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;">
        <h3 style="margin:0;">设为隐私记忆</h3>
        <button onclick="this.closest('.modal-overlay').remove()" style="background:none;border:none;font-size:20px;cursor:pointer;color:var(--text-dim);">&times;</button>
      </div>
      <p style="font-size:13px;color:var(--text-dim);margin-bottom:16px;">设置密码后，前端列表中将隐藏内容预览，查看完整内容需要输入密码。</p>
      <input type="password" id="privacy-lock-password" placeholder="设置密码（至少3位）" style="width:100%;padding:10px 14px;border-radius:12px;border:1px solid var(--border);margin-bottom:8px;background:var(--surface);color:var(--text);font-family:inherit;" />
      <input type="password" id="privacy-lock-confirm" placeholder="确认密码" style="width:100%;padding:10px 14px;border-radius:12px;border:1px solid var(--border);margin-bottom:12px;background:var(--surface);color:var(--text);font-family:inherit;" />
      <div id="privacy-lock-error" style="color:var(--negative);font-size:12px;margin-bottom:12px;min-height:16px;"></div>
      <button onclick="confirmPrivacyLock('${bucketId}')" style="width:100%;padding:10px;border:none;background:var(--accent);color:white;border-radius:12px;cursor:pointer;font-size:13px;">确认锁定</button>
    </div>
  `;
  document.body.appendChild(modal);
  modal.querySelector('#privacy-lock-password').focus();
}

async function confirmPrivacyLock(bucketId) {
  var pwd = document.getElementById('privacy-lock-password').value;
  var confirmPwd = document.getElementById('privacy-lock-confirm').value;
  var errorEl = document.getElementById('privacy-lock-error');

  if (!pwd || pwd.length < 3) {
    errorEl.textContent = '密码至少3位';
    return;
  }
  if (pwd !== confirmPwd) {
    errorEl.textContent = '两次密码不一致';
    return;
  }

  try {
    var res = await authFetch('/api/bucket/' + bucketId + '/privacy', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ is_private: true, password: pwd })
    });
    if (!res) return;
    var data = await res.json();
    if (data.success) {
      document.querySelector('.modal-overlay').remove();
      showDetail(bucketId);
      loadBuckets();
    } else {
      errorEl.textContent = data.error || '锁定失败';
    }
  } catch (e) {
    errorEl.textContent = '锁定失败: ' + e.message;
  }
}

async function togglePrivacyLock(bucketId, lock) {
  if (lock) {
    showPrivacyLockDialog(bucketId);
    return;
  }
  // Unlock
  if (!confirm('确定要解除这条记忆的隐私锁定吗？')) return;
  try {
    var res = await authFetch('/api/bucket/' + bucketId + '/privacy', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ is_private: false, password: '' })
    });
    if (!res) return;
    var data = await res.json();
    if (data.success) {
      showDetail(bucketId);
      loadBuckets();
    } else {
      alert('解锁失败: ' + (data.error || '未知错误'));
    }
  } catch (e) {
    alert('解锁失败: ' + e.message);
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
    var res = await fetch(BASE + '/api/network', { credentials: 'include' });
    var data = await res.json();
    // --- Assemble nodes & edges from identities / self-profile / event chains ---
    // --- 组装节点与边（身份 / 自我认知 / 事件链实体图谱） ---
    var nodes = [];
    if (data.self_profile) {
      nodes.push({
        id: data.self_profile.id,
        name: data.self_profile.name,
        type: 'identity',
        is_self: true,
        score: 1.0,
        pinned: true
      });
    }
    (data.identities || []).forEach(function(i) {
      nodes.push({
        id: i.id,
        name: i.name,
        type: 'identity',
        is_self: false,
        score: 0.6 + Math.min(0.4, (i.activation_count || 0) / 20)
      });
    });
    (data.chains || []).forEach(function(c) {
      nodes.push({
        id: c.id,
        name: c.topic,
        type: 'chain',
        score: 0.7
      });
    });
    var edges = (data.edges || []).map(function(e) {
      return {
        source: e.from_id,
        target: e.to_id,
        type: 'related',
        weight: Math.max(0.3, (e.effective_weight || e.base_weight || 5) / 5)
      };
    });
    (data.chains || []).forEach(function(c) {
      (c.related_chain_ids || []).forEach(function(rid) {
        edges.push({ source: c.id, target: rid, type: 'chain_related', weight: 0.8 });
      });
    });
    networkData = { nodes: nodes, edges: edges };
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
    chain: '#3A6EA5',
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
    chain: '事件链',
  },
  edgeColors: {
    same_event: '#2196F3',
    related: '#4CAF50',
    hierarchy: '#FF9800',
    similarity: '#9C27B0',
    cooccurrence: '#E91E63',
    chain_related: '#795548',
  },
  edgeLabels: {
    same_event: '同一事件',
    related: '相关',
    hierarchy: '层级',
    similarity: '相似',
    cooccurrence: '共享标签',
    chain_related: '链间关联',
  },
  edgeDashed: {
    same_event: false,
    related: false,
    hierarchy: true,
    similarity: false,
    cooccurrence: true,
    chain_related: true,
  },
  typeOrder: ['identity', 'chain', 'pattern', 'permanent', 'event', 'feel', 'experience', 'candlestick', 'dynamic'],
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

function showLoading(container) {
  if (typeof container === 'string') container = document.getElementById(container);
  if (container) container.innerHTML = '<div class="empty-state" style="border:none;">加载中...</div>';
}

function showError(container, msg) {
  if (typeof container === 'string') container = document.getElementById(container);
  if (container) container.innerHTML = '<div class="empty-state" style="border-color:rgba(244,67,54,0.4);color:#f44336;">加载失败: ' + escapeHtml(msg) + '</div>';
}

function formatTimeAgo(iso) {
  if (!iso) return '—';
  var d = new Date(iso);
  var now = new Date();
  var diffMs = now - d;
  var hours = Math.floor(diffMs / 3600000);
  
  if (hours < 1) {
    var mins = Math.floor(diffMs / 60000);
    return mins < 1 ? '刚刚' : mins + '分钟前';
  }
  if (hours < 24) return hours + '小时前';
  
  var days = Math.floor(hours / 24);
  if (days === 1) return '昨天';
  if (days < 7) return days + '天前';
  
  return d.getMonth() + 1 + '/' + d.getDate() + ' ' + d.getHours().toString().padStart(2, '0') + ':' + d.getMinutes().toString().padStart(2, '0');
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
    var res = await fetch(BASE + '/api/config', { credentials: 'include' });
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
    var res = await fetch(BASE + '/api/config', { credentials: 'include' });
    var cfg = await res.json();
    document.getElementById('cfg-dehy-model').value = cfg.dehydration.model || '';
    document.getElementById('cfg-dehy-url').value = cfg.dehydration.base_url || '';
    var dehyKeyMasked = cfg.dehydration.api_key_masked || '';
    document.getElementById('cfg-dehy-key').placeholder = '当前: ' + (dehyKeyMasked || '未设置');
    document.getElementById('cfg-dehy-key').value = dehyKeyMasked ? '******' : '';
    // --- Embedding config ---
    var emb = cfg.embedding || {};
    document.getElementById('cfg-emb-enabled').value = emb.enabled ? 'true' : 'false';
    document.getElementById('cfg-emb-url').value = emb.base_url || '';
    document.getElementById('cfg-emb-model').value = emb.model || '';
    var embKeyMasked = emb.api_key_masked || '';
    document.getElementById('cfg-emb-key').placeholder = '当前: ' + (embKeyMasked || '未设置');
    document.getElementById('cfg-emb-key').value = embKeyMasked ? '******' : '';
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
    embedding: {
      enabled: document.getElementById('cfg-emb-enabled').value === 'true',
      model: document.getElementById('cfg-emb-model').value,
      base_url: document.getElementById('cfg-emb-url').value,
    },
    persist: persist,
  };
  var dehyKeyVal = document.getElementById('cfg-dehy-key').value;
  if (dehyKeyVal && dehyKeyVal !== '******') body.dehydration.api_key = dehyKeyVal;
  var embKeyVal = document.getElementById('cfg-emb-key').value;
  if (embKeyVal && embKeyVal !== '******') body.embedding.api_key = embKeyVal;

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

async function testEmbeddingConnection() {
  var btn = document.getElementById('btn-emb-test');
  var status = document.getElementById('emb-status');
  btn.disabled = true;
  btn.style.opacity = '0.6';
  status.innerHTML = '<span style="color:var(--warning)">测试中...</span>';
  try {
    var res = await fetch(BASE + '/api/embedding-test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      credentials: 'include'
    });
    var result = await res.json();
    if (result.ok) {
      status.innerHTML = '<span style="color:var(--positive)">✓ 向量连接正常</span>';
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
      credentials: 'include'
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
  var fileInput = document.getElementById('import-file');
  var overwrite = document.getElementById('import-overwrite').checked;

  if (!fileInput.files || !fileInput.files[0]) {
    status.innerHTML = '<span style="color:var(--warning)">请选择 .zip 文件</span>';
    return;
  }

  var file = fileInput.files[0];
  status.innerHTML = '<span style="color:var(--warning)">导入中... (' + file.name + ')</span>';

  try {
    var formData = new FormData();
    formData.append('file', file);
    formData.append('overwrite', overwrite ? 'true' : 'false');

    var res = await fetch(BASE + '/api/import-brain', {
      method: 'POST',
      body: formData,
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
    loadExpiringMemories();
    checkAIStatus();
  }
});

// Clear search input on load (prevents browser autofill showing password)
function clearSearchAutofill() {
  var si = document.getElementById('search-input');
  if (si && si.value) {
    si.value = '';
  }
}
setTimeout(clearSearchAutofill, 100);
setTimeout(clearSearchAutofill, 500);
document.addEventListener('visibilitychange', function() {
  if (!document.hidden) {
    setTimeout(clearSearchAutofill, 100);
  }
});

async function doRegenerateNames() {
  var status = document.getElementById('regenerate-status');
  if (!confirm('确定要重新生成所有记忆名称吗？这将覆盖现有名称，建议先导出备份。')) return;

  var bucketIds = allBuckets.map(function(b) { return b.id; });
  if (!bucketIds.length) {
    status.innerHTML = '<span style="color:var(--text-dim)">没有记忆桶需要处理</span>';
    return;
  }

  var total = bucketIds.length;
  var completed = 0;
  var succeeded = 0;
  var failed = 0;

  status.innerHTML = '<div style="display:flex;flex-direction:column;gap:8px;">' +
    '<span style="color:var(--warning)">正在重新生成名称...</span>' +
    '<div style="height:4px;background:var(--border);border-radius:2px;overflow:hidden;">' +
    '<div id="regenerate-progress-bar" style="height:100%;background:var(--accent);width:0%;transition:width 0.3s ease;border-radius:2px;"></div>' +
    '</div>' +
    '<span style="font-size:12px;color:var(--text-dim);">处理进度: <span id="regenerate-counter">0</span>/' + total + '</span>' +
    '<span style="font-size:12px;color:var(--text-dim);" id="regenerate-details">准备开始...</span>' +
    '</div>';

  try {
    var results = [];
    for (var i = 0; i < bucketIds.length; i++) {
      var bid = bucketIds[i];
      var bucket = null;
      
      try {
        var bucketResp = await authFetch('/api/bucket/' + bid);
        if (bucketResp) bucket = await bucketResp.json();
      } catch(e) {
        console.warn('Failed to get bucket:', bid);
      }

      if (!bucket || !bucket.content) {
        completed++;
        failed++;
      } else {
        try {
          var resp = await authFetch('/api/regenerate-names', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({bucket_ids: [bid]})
          });

          if (resp) {
            var result = await resp.json();
            if (result.ok && result.succeeded > 0) {
              succeeded++;
              var newName = result.results && result.results[0] ? result.results[0].new_name : '';
              document.getElementById('regenerate-details').innerHTML = '正在处理: <span style="color:var(--accent);">' + (newName || bid.substring(0,8)) + '</span>';
            } else {
              failed++;
            }
          } else {
            failed++;
          }
        } catch(e) {
          failed++;
        }
        completed++;
      }

      var progress = Math.round((completed / total) * 100);
      document.getElementById('regenerate-progress-bar').style.width = progress + '%';
      document.getElementById('regenerate-counter').textContent = completed;
    }

    status.innerHTML = '<div style="display:flex;flex-direction:column;gap:8px;">' +
      '<span style="color:var(--positive)">✓ 完成！</span>' +
      '<div style="height:4px;background:var(--border);border-radius:2px;overflow:hidden;">' +
      '<div style="height:100%;background:var(--accent);width:100%;border-radius:2px;"></div>' +
      '</div>' +
      '<span style="font-size:12px;color:var(--text-dim);">总处理: ' + total + ' | 成功: <span style="color:var(--positive);">' + succeeded + '</span> | 失败: <span style="color:var(--negative);">' + failed + '</span></span>' +
      '</div>';

    invalidateCache('buckets');
    loadBuckets();
  } catch(e) {
    status.innerHTML = '<span style="color:var(--negative)">✗ 请求失败: ' + e.message + '</span>';
  }
}

// ========================================
// Bucket list batch operations / 记忆桶批量操作
// ========================================
function toggleBucketSelection(bucketId) {
  if (selectedBuckets.has(bucketId)) {
    selectedBuckets.delete(bucketId);
  } else {
    selectedBuckets.add(bucketId);
  }
  updateBatchToolbar();
}

function clearBatchSelection() {
  selectedBuckets.clear();
  var checkboxes = document.querySelectorAll('.bucket-checkbox');
  checkboxes.forEach(function(cb) { cb.checked = false; });
  updateBatchToolbar();
}

function updateBatchToolbar() {
  var toolbar = document.getElementById('batchToolbar');
  var countEl = document.getElementById('batchCount');
  if (!toolbar) return;
  if (selectedBuckets.size > 0) {
    toolbar.style.display = 'flex';
    if (countEl) countEl.textContent = '已选 ' + selectedBuckets.size + ' 项';
  } else {
    toolbar.style.display = 'none';
  }
}

async function batchSetResolved() {
  if (selectedBuckets.size === 0) return;
  var ids = Array.from(selectedBuckets);
  var succeeded = 0;
  var failed = 0;
  for (var i = 0; i < ids.length; i++) {
    try {
      var resp = await authFetch(BASE + '/api/bucket/' + ids[i], {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({resolved: 1})
      });
      if (resp && resp.ok) {
        succeeded++;
      } else {
        failed++;
      }
    } catch(e) {
      failed++;
    }
  }
  alert('批量沉底完成：成功 ' + succeeded + ' 项，失败 ' + failed + ' 项');
  clearBatchSelection();
  invalidateCache('buckets');
  loadBuckets();
  loadExpiringMemories();
}

async function batchDelete() {
  var ids = Array.from(selectedBuckets);
  if (ids.length === 0) return;

  if (!confirm('确定要删除选中的 ' + ids.length + ' 条记忆吗？')) {
    return;
  }

  var btn = document.getElementById('batch-delete-btn');
  if (btn) btn.innerHTML = '删除中...';

  try {
    var resp = await authFetch(BASE + '/api/buckets/batch', {
      method: 'DELETE',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ids: ids})
    });
    if (!resp) return;
    var data = await resp.json();
    if (data.success) {
      selectedBuckets.clear();
      updateBatchToolbar();
      invalidateCache('buckets');
      if (btn) btn.innerHTML = '删除完成';
      loadBuckets();
      loadExpiringMemories();
    } else {
      alert('删除失败: ' + (data.error || '未知错误'));
      if (btn) btn.innerHTML = '删除选中';
    }
  } catch(e) {
    alert('删除失败: ' + e.message);
    if (btn) btn.innerHTML = '删除选中';
  }
}

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
  // --- 自动触发：无需手动点击，静默扫描高情绪记忆并刷新锚点列表 ---
  // --- auto-trigger: silently scan high-emotion buckets and refresh the list ---
  const threshold = 0.6;
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
    await resp.json();
    loadAnchors();
  } catch(e) {
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
      <div style="width:44px;height:44px;border-radius:12px;background:#FFB74D;display:flex;align-items:center;justify-content:center;color:#fff;font-size:18px;font-weight:600;">${candlesticks.length}</div>
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
            <h3 style="margin:0;font-size:17px;font-weight:600;color:var(--text);">${escapeHtml(candle.title || candle.content.substring(0, 20))}</h3>
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
    showLoading('candlestick-list');
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
    showError('candlestick-list', e.message);
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
    if (!resp.ok) {
      const errData = await resp.json().catch(() => ({}));
      alert('删除失败: ' + (errData.error || `HTTP ${resp.status}`));
      return;
    }
    
    invalidateCache('candlesticks');
    loadCandlesticks();
  } catch(e) {
    alert('删除失败: ' + e.message);
  }
}

// ========================================
// Cycle / 例假周期功能
// ========================================

async function loadCycle() {
  const list = document.getElementById('cycle-list');
  const summary = document.getElementById('cycle-summary');
  try {
    const resp = await authFetch('/api/cycle');
    if (!resp) return;
    const data = await resp.json();
    renderCycleSummary(data.summary);
    renderCycleList(data.records);
  } catch(e) {
    showError('cycle-list', e.message);
  }
}

function renderCycleSummary(summary) {
  const container = document.getElementById('cycle-summary');
  const daysUntil = summary.days_until_next;
  let statusColor = 'var(--text)';
  let statusText = '---';
  let statusBadge = '';
  
  if (daysUntil !== null) {
    if (daysUntil === 0) {
      statusColor = 'var(--negative)';
      statusText = '今天';
      statusBadge = '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--negative);margin-right:8px;"></span>';
    } else if (daysUntil === 1) {
      statusColor = 'var(--warning)';
      statusText = '明天';
      statusBadge = '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--warning);margin-right:8px;"></span>';
    } else if (daysUntil <= 5) {
      statusColor = 'var(--warning)';
      statusText = daysUntil + '天后';
      statusBadge = '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--warning);margin-right:8px;"></span>';
    } else {
      statusText = daysUntil + '天后';
    }
  }

  // --- Current phase card / 当前相位卡片 ---
  const phaseMap = {
    period: { text: '经期', color: 'var(--negative)' },
    pre_period: { text: '经前', color: 'var(--warning)' },
    follicular: { text: '安全期', color: 'var(--positive)' },
    unknown: { text: '---', color: 'var(--text-dim)' }
  };
  const ph = phaseMap[summary.phase] || phaseMap.unknown;

  container.innerHTML = `
    <div style="background:var(--surface);border-radius:var(--radius-lg);padding:18px;border:1px solid var(--border);">
      <div style="font-size:12px;color:var(--text-dim);margin-bottom:6px;">当前相位</div>
      <div style="font-size:28px;font-weight:600;color:${ph.color};">${ph.text}</div>
      ${summary.phase === 'pre_period' ? '<div style="font-size:12px;color:var(--text-dim);margin-top:6px;">情绪易波动，注意休息</div>' : ''}
      ${summary.phase === 'period' ? '<div style="font-size:12px;color:var(--text-dim);margin-top:6px;">情绪易敏感，注意保暖</div>' : ''}
    </div>
    <div style="background:var(--surface);border-radius:var(--radius-lg);padding:18px;border:1px solid var(--border);">
      <div style="font-size:12px;color:var(--text-dim);margin-bottom:6px;">距离下次</div>
      <div style="font-size:28px;font-weight:600;color:${statusColor};display:flex;align-items:center;">${statusBadge}${statusText}</div>
    </div>
    <div style="background:var(--surface);border-radius:var(--radius-lg);padding:18px;border:1px solid var(--border);">
      <div style="font-size:12px;color:var(--text-dim);margin-bottom:6px;">平均周期</div>
      <div style="font-size:28px;font-weight:600;color:var(--accent);">${summary.average_cycle_days || '---'} 天</div>
    </div>
    <div style="background:var(--surface);border-radius:var(--radius-lg);padding:18px;border:1px solid var(--border);">
      <div style="font-size:12px;color:var(--text-dim);margin-bottom:6px;">预测日期</div>
      <div style="font-size:20px;font-weight:600;color:var(--text);">${summary.predicted_next_date || '---'}</div>
    </div>
    <div style="background:var(--surface);border-radius:var(--radius-lg);padding:18px;border:1px solid var(--border);">
      <div style="font-size:12px;color:var(--text-dim);margin-bottom:6px;">总记录数</div>
      <div style="font-size:28px;font-weight:600;color:var(--positive);">${summary.total_records} 次</div>
    </div>
  `;
}

function renderCycleList(records) {
  const list = document.getElementById('cycle-list');
  const empty = document.getElementById('cycle-empty');
  
  if (!records || records.length === 0) {
    list.innerHTML = '';
    empty.style.display = '';
    return;
  }
  
  empty.style.display = 'none';
  
  const sortedRecords = [...records].sort((a, b) => b.date_timestamp - a.date_timestamp);
  
  let html = '';
  sortedRecords.forEach(record => {
    const flowText = {
      'light': '少量',
      'normal': '正常',
      'heavy': '大量'
    }[record.flow_level] || record.flow_level;
    
    html += `
      <div style="background:var(--surface);border-radius:var(--radius-md);padding:16px;border:1px solid var(--border);margin-bottom:10px;position:relative;border-left:3px solid var(--negative);">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px;">
          <div>
            <div style="font-weight:600;font-size:14px;color:var(--text);">${record.start_date}</div>
            <div style="font-size:12px;color:var(--text-dim);margin-top:2px;">
              持续 ${record.duration} 天 · 流量 ${flowText}
              ${record.pain_level > 0 ? ' · 疼痛 ' + record.pain_level + '/10' : ''}
            </div>
          </div>
          <button onclick="deleteCycleRecord(decodeURIComponent('${encodeURIComponent(record.start_date)}'))" 
            style="background:none;border:none;color:var(--text-dim);cursor:pointer;padding:4px 8px;border-radius:var(--radius-sm);transition:all 0.2s;"
            onmouseover="this.style.color='var(--negative)';this.style.background='rgba(255,100,100,0.1)'"
            onmouseout="this.style.color='var(--text-dim)';this.style.background='none'">
            删除
          </button>
        </div>
        ${record.symptoms ? `<div style="font-size:13px;color:var(--text);margin-bottom:8px;">症状: ${record.symptoms}</div>` : ''}
        ${record.notes ? `<div style="font-size:12px;color:var(--text-dim);background:var(--surface-solid);padding:8px;border-radius:var(--radius-sm);">${record.notes}</div>` : ''}
      </div>
    `;
  });
  
  list.innerHTML = html;
}

async function deleteCycleRecord(startDate) {
  if (!confirm('确定要删除这条记录吗？')) return;
  try {
    const res = await authFetch(`/api/cycle/${startDate}`, { method: 'DELETE' });
    const data = await res.json();
    if (data.success) {
      renderCycleSummary(data.summary);
      loadCycle();
    } else {
      alert('删除失败: ' + (data.error || '未知错误'));
    }
  } catch (e) {
    alert('删除失败: ' + e.message);
  }
}

// ============ Journal / 日记 ============
async function loadJournal() {
  const listEl = document.getElementById('journal-list');
  if (!listEl) return;
  listEl.innerHTML = '<div style="padding:12px;color:var(--text-dim);font-size:13px;">加载中...</div>';
  try {
    const res = await authFetch('/api/journal/list?limit=50');
    if (!res) return;
    const data = await res.json();
    const entries = data.entries || [];
    renderJournalList(entries);
    // --- Default: show today's entry (or empty state) ---
    // --- 默认显示今天（或空状态） ---
    const today = new Date().toISOString().slice(0, 10);
    const entry = entries.find(function(e) { return e.date === today; });
    showJournalDetail(today, entry || null);
  } catch (e) {
    listEl.innerHTML = '<div style="padding:12px;color:var(--negative);font-size:13px;">加载失败: ' + escapeHtml(e.message) + '</div>';
  }
}

function renderJournalList(entries) {
  const listEl = document.getElementById('journal-list');
  if (!listEl) return;
  if (!entries.length) {
    listEl.innerHTML = '<div style="padding:12px;color:var(--text-dim);font-size:13px;">暂无日记，点击右上角新建</div>';
    return;
  }
  let html = '';
  entries.forEach(function(e) {
    const date = e.date || '';
    const tags = e.emotion_tags || '';
    html += '<div class="journal-item" data-date="' + date + '" onclick="selectJournal(\'' + date + '\')" ' +
      'style="padding:10px;border-radius:var(--radius-sm);cursor:pointer;margin-bottom:4px;transition:background 0.2s;border:1px solid transparent;">' +
      '<div style="font-weight:600;font-size:13px;color:var(--text);">' + date + '</div>' +
      (tags ? '<div style="font-size:12px;color:var(--text-dim);margin-top:2px;">' + escapeHtml(tags) + '</div>' : '') +
    '</div>';
  });
  listEl.innerHTML = html;
}

function selectJournal(date) {
  document.querySelectorAll('.journal-item').forEach(function(el) {
    el.style.background = el.dataset.date === date ? 'var(--accent-glow)' : '';
    el.style.borderColor = el.dataset.date === date ? 'var(--border-strong)' : 'transparent';
  });
  authFetch('/api/journal?date=' + encodeURIComponent(date)).then(function(res) {
    if (!res) return null;
    return res.json();
  }).then(function(data) {
    if (data) showJournalDetail(date, data.entry || null);
  }).catch(function() {
    showJournalDetail(date, null);
  });
}

function showJournalDetail(date, entry) {
  const detailEl = document.getElementById('journal-detail');
  if (!detailEl) return;
  if (!entry) {
    detailEl.innerHTML = '<div style="color:var(--text-dim);text-align:center;padding:60px 0;">' +
      '<div style="font-size:18px;font-weight:600;color:var(--text);margin-bottom:8px;">' + date + '</div>' +
      '这一天还没有日记' +
      '<div style="margin-top:16px;"><button class="btn-secondary" onclick="showJournalEditor(\'' + date + '\')">新建日记</button></div>' +
    '</div>';
    return;
  }
  const tags = entry.emotion_tags || '';
  const mood = entry.mood_comment || '';
  const summary = entry.event_summary || '';
  detailEl.innerHTML = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">' +
    '<h3 style="font-size:18px;font-weight:600;">' + (entry.date || date) + '</h3>' +
    '<button class="btn-secondary" onclick="showJournalEditor(\'' + (entry.date || date) + '\')">编辑</button>' +
  '</div>' +
  '<div class="field"><label>事件摘要</label><div style="white-space:pre-wrap;line-height:1.7;">' + escapeHtml(summary) + '</div></div>' +
  (mood ? '<div class="field"><label>情绪点评</label><div style="white-space:pre-wrap;line-height:1.7;color:var(--accent);">' + escapeHtml(mood) + '</div></div>' : '') +
  (tags ? '<div class="field"><label>情绪标签</label><div>' + tags.split(',').map(function(t) {
    t = t.trim();
    if (!t) return '';
    return '<span style="display:inline-block;padding:2px 10px;border-radius:12px;background:var(--accent-glow);color:var(--accent);font-size:12px;margin-right:6px;margin-bottom:4px;">' + escapeHtml(t) + '</span>';
  }).join('') + '</div></div>' : '');
}

function showJournalEditor(date) {
  document.getElementById('journal-editor-title').textContent = date ? '编辑日记' : '新建日记';
  const today = new Date().toISOString().slice(0, 10);
  const d = date || today;
  document.getElementById('journal-date').value = d;
  document.getElementById('journal-summary').value = '';
  document.getElementById('journal-mood').value = '';
  document.getElementById('journal-tags').value = '';
  document.getElementById('journal-editor-msg').textContent = '';
  authFetch('/api/journal?date=' + encodeURIComponent(d)).then(function(res) {
    if (!res) return null;
    return res.json();
  }).then(function(data) {
    if (data && data.entry) {
      document.getElementById('journal-summary').value = data.entry.event_summary || '';
      document.getElementById('journal-mood').value = data.entry.mood_comment || '';
      document.getElementById('journal-tags').value = data.entry.emotion_tags || '';
    }
    document.getElementById('journal-editor-modal').style.display = 'flex';
  }).catch(function() {
    document.getElementById('journal-editor-modal').style.display = 'flex';
  });
}

function closeJournalEditor() {
  document.getElementById('journal-editor-modal').style.display = 'none';
}

async function saveJournal() {
  const date = document.getElementById('journal-date').value;
  const summary = document.getElementById('journal-summary').value;
  const mood = document.getElementById('journal-mood').value;
  const tags = document.getElementById('journal-tags').value;
  const msgEl = document.getElementById('journal-editor-msg');
  if (!date) {
    msgEl.textContent = '请选择日期';
    msgEl.style.color = 'var(--negative)';
    return;
  }
  if (!summary && !mood) {
    msgEl.textContent = '摘要和情绪点评至少填一项';
    msgEl.style.color = 'var(--negative)';
    return;
  }
  try {
    const resp = await authFetch('/api/journal', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ date: date, event_summary: summary, mood_comment: mood, emotion_tags: tags })
    });
    if (!resp) return;
    const data = await resp.json();
    if (data.success) {
      closeJournalEditor();
      loadJournal();
    } else {
      msgEl.textContent = '保存失败: ' + (data.error || '未知错误');
      msgEl.style.color = 'var(--negative)';
    }
  } catch (e) {
    msgEl.textContent = '保存失败: ' + e.message;
    msgEl.style.color = 'var(--negative)';
  }
}

function showCycleEditor() {
  document.getElementById('cycle-start-date').valueAsDate = new Date();
  document.getElementById('cycle-duration').value = '';
  document.getElementById('cycle-pain').value = '';
  document.getElementById('cycle-flow').value = 'normal';
  document.getElementById('cycle-symptoms').value = '';
  document.getElementById('cycle-notes').value = '';
  document.getElementById('cycle-editor-msg').textContent = '';
  document.getElementById('cycle-editor-modal').style.display = 'flex';
}

function closeCycleEditor() {
  document.getElementById('cycle-editor-modal').style.display = 'none';
}

async function saveCycle() {
  const startDate = document.getElementById('cycle-start-date').value;
  const duration = document.getElementById('cycle-duration').value;
  const pain = document.getElementById('cycle-pain').value;
  const flow = document.getElementById('cycle-flow').value;
  const symptoms = document.getElementById('cycle-symptoms').value;
  const notes = document.getElementById('cycle-notes').value;
  
  if (!startDate) {
    document.getElementById('cycle-editor-msg').textContent = '请选择开始日期';
    document.getElementById('cycle-editor-msg').style.color = 'var(--negative)';
    return;
  }
  
  try {
    const resp = await authFetch('/api/cycle', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        start_date: startDate,
        duration: duration ? parseInt(duration) : 5,
        pain_level: pain ? parseInt(pain) : 0,
        flow_level: flow,
        symptoms: symptoms,
        notes: notes
      })
    });
    if (!resp) return;
    
    const data = await resp.json();
    if (data.success) {
      closeCycleEditor();
      loadCycle();
    } else {
      document.getElementById('cycle-editor-msg').textContent = data.error || '保存失败';
      document.getElementById('cycle-editor-msg').style.color = 'var(--negative)';
    }
  } catch(e) {
    document.getElementById('cycle-editor-msg').textContent = '保存失败: ' + e.message;
    document.getElementById('cycle-editor-msg').style.color = 'var(--negative)';
  }
}

// ========================================
// Analytics / 数据分析功能
// ========================================

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
            <button onclick="showDetail('${exp.id}')" style="padding:6px 14px;border-radius:10px;border:1px solid var(--border);background:var(--surface);cursor:pointer;font-size:12px;color:var(--text-secondary);transition:all 0.2s;">查看</button>
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
    showLoading('experience-list');
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
    showError('experience-list', e.message);
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

// 当前已加载的名册列表，用于创建时扫视查重
let _identityList = [];

function renderIdentities(identities) {
  const list = document.getElementById('identity-list');
  const empty = document.getElementById('identity-empty');
  
  if (identities.length === 0) {
    list.innerHTML = '';
    empty.style.display = '';
    return;
  }
  empty.style.display = 'none';
  list.innerHTML = identities.map(i => {
    // --- 激活次数 → 重要度：次数越多越重要 ---
    // --- activation count → importance tier ---
    const act = i.activation_count || 0;
    let importanceBadge = '';
    if (act >= 10) importanceBadge = '<span style="background:rgba(47,79,79,0.12);color:var(--accent);padding:3px 10px;border-radius:12px;font-size:11px;font-weight:600;">重要</span>';
    else if (act >= 3) importanceBadge = '<span style="background:rgba(74,124,89,0.12);color:#4A7C59;padding:3px 10px;border-radius:12px;font-size:11px;font-weight:600;">熟悉</span>';
    else importanceBadge = '<span style="background:rgba(176,168,152,0.15);color:var(--text-dim);padding:3px 10px;border-radius:12px;font-size:11px;font-weight:500;">普通</span>';
    return `
    <div class="identity-card">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:16px;">
        <div>
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
            <h3 style="margin:0;font-size:18px;">${escapeHtml(i.name || i.topic || '未命名')}</h3>
            ${i.relation_tags && i.relation_tags.length > 0 ? i.relation_tags.map(t => `<span class="identity-tag">${escapeHtml(t)}</span>`).join('') : ''}
          </div>
          <div style="display:flex;align-items:center;gap:8px;margin-top:6px;">
            ${importanceBadge}
            <span style="font-size:11px;color:var(--text-dim);">激活 ${act} 次</span>
          </div>
          ${i.aliases && i.aliases.length > 0 ? `<div style="font-size:13px;color:var(--text-dim);margin-top:4px;">别名: ${escapeHtml(i.aliases.join(', '))}</div>` : ''}
        </div>
        <div style="display:flex;gap:8px;">
          <button onclick="showIdentityEditor('${i.id}')" style="padding:6px 12px;border:none;background:var(--accent);color:white;border-radius:8px;cursor:pointer;font-size:12px;">编辑</button>
          <button onclick="deleteIdentity('${i.id}')" style="padding:6px 12px;border:none;background:var(--negative);color:white;border-radius:8px;cursor:pointer;font-size:12px;">删除</button>
        </div>
      </div>
      ${i.relations && i.relations.length > 0 ? `
        <div style="margin-bottom:12px;">
          <div style="font-size:12px;color:var(--text-dim);font-weight:500;margin-bottom:6px;">关系</div>
          <div>${i.relations.map(r => `<span style="background:var(--accent-glow);color:var(--accent);padding:3px 10px;border-radius:12px;font-size:12px;margin-right:6px;margin-bottom:4px;display:inline-block;">${escapeHtml(r.relation_type || '朋友')} · ${escapeHtml(r.target_name || '')}</span>`).join('')}</div>
        </div>
      ` : ''}
      
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
      
      ${(i.traits || i.core_traits) && (i.traits || i.core_traits).length > 0 ? `
        <div style="margin-bottom:12px;">
          <div style="font-size:12px;color:var(--text-dim);font-weight:500;margin-bottom:6px;">性格特征</div>
          <div>${(i.traits || i.core_traits).map(t => `<span style="background:#4A7C5920;color:#4A7C59;padding:4px 10px;border-radius:12px;font-size:13px;margin-right:6px;margin-bottom:4px;display:inline-block;">${escapeHtml(t)}</span>`).join('')}</div>
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
  `;
  }).join('');
}

async function loadIdentities() {
  const list = document.getElementById('identity-list');
  try {
    const resp = await authFetch('/api/identities');
    if (!resp) return;
    const data = await resp.json();
    const identities = data.identities || [];
    _identityList = identities;
    renderSelfProfile(data.self_profile || null);
    renderIdentities(identities);
    loadRelationMap();
  } catch(e) {
    list.innerHTML = `<p style="color:var(--negative)">加载失败: ${e.message}</p>`;
  }
}

// ========================================
// 人际关系地图：以 AI 自我认知为中心，人物为节点，关系类型为连线
// ========================================
const RELATION_COLORS = {
  '恋人': '#C0392B', '配偶': '#B03A2E', '家人': '#D35400', '父母': '#E67E22',
  '子女': '#E67E22', '兄弟姐妹': '#F39C12', '亲戚': '#D4AC0D',
  '挚友': '#16A085', '朋友': '#27AE60', '同事': '#2980B9', '同学': '#2E86C1',
  '师生': '#8E44AD', '领导': '#7D3C98', '下属': '#A569BD', '合作伙伴': '#34495E',
  '网友': '#95A5A6', '其他': '#7F8C8D'
};

async function loadRelationMap() {
  const container = document.getElementById('relation-map');
  if (!container) return;
  try {
    const resp = await authFetch('/api/relations');
    if (!resp) return;
    const data = await resp.json();
    const identities = data.identities || [];
    const edges = data.edges || [];
    const self = data.self_profile || null;
    if (identities.length === 0) {
      container.innerHTML = '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:360px;gap:8px;color:var(--text-dim);font-size:13px;">' +
        '<div>暂无人物档案</div>' +
        '<div style="font-size:12px;">点击右上角「+ 创建身份」录入人物后，这里会自动生成人际关系地图</div>' +
        '</div>';
      return;
    }
    // --- 中心 = AI 自身（即自我认知档案）；地图只是关系展示，不承载填写职责 ---
    // --- center = AI itself; the map only visualizes relations ---
    const centerName = (self && self.name) ? self.name : '我';
    const nodes = [{ id: self ? self.id : '__self__', name: centerName, isSelf: true, act: (self && self.activation_count) || 0 }];
    identities.forEach(p => nodes.push({
      id: p.id, name: p.name, isSelf: false, act: p.activation_count || 0,
      connected: edges.some(e => e.from_id === p.id || e.to_id === p.id)
    }));

    const W = Math.max(container.clientWidth || 720, 400);
    const H = 380;
    const cx = W / 2, cy = H / 2;
    // --- 分层：有关系的人物靠内圈，尚无关系的人物散在外圈 ---
    // --- layering: connected people inner ring, unconnected outer ring ---
    const inner = nodes.filter(n => !n.isSelf && n.connected);
    const outer = nodes.filter(n => !n.isSelf && !n.connected);
    const total = Math.max(inner.length + outer.length, 1);
    const R = Math.min(cx, cy) - (total <= 2 ? 90 : 64);
    const place = (list, radius, startAngle) => {
      list.forEach((n, i) => {
        const angle = startAngle + (i * 2 * Math.PI / Math.max(list.length, 1));
        n.x = cx + radius * Math.cos(angle);
        n.y = cy + radius * Math.sin(angle);
      });
    };
    place(inner, R * 0.78, -Math.PI / 2);
    place(outer, R, -Math.PI / 2 + Math.PI / Math.max(outer.length + 1, 2));
    const centerNode = nodes.find(n => n.isSelf);
    centerNode.x = cx; centerNode.y = cy;

    // --- 边：关系类型文字标在线中点 ---
    // --- edges: relation-type label at line midpoint ---
    const edgeParts = edges.map(e => {
      const a = nodes.find(n => n.id === e.from_id) || centerNode;
      const b = nodes.find(n => n.id === e.to_id) || centerNode;
      if (!a || !b) return '';
      const color = RELATION_COLORS[e.relation_type] || '#7F8C8D';
      const midX = (a.x + b.x) / 2, midY = (a.y + b.y) / 2;
      return `<line x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" stroke="${color}" stroke-width="1.6" stroke-opacity="0.6" />
        <text x="${midX}" y="${midY - 6}" text-anchor="middle" font-size="11" fill="${color}" font-weight="500" paint-order="stroke" stroke="#F7F5F0" stroke-width="4">${escapeHtml(e.relation_type)}</text>`;
    }).join('');

    // --- 节点：重要的人（激活多）圆更大；标签上下交替防重叠；点击打开编辑 ---
    // --- nodes: higher activation = bigger circle; labels alternate up/down; click to edit ---
    const nodeParts = nodes.map((n, idx) => {
      const size = n.isSelf ? 34 : (14 + Math.min(n.act * 2, 16));
      const color = n.isSelf ? '#2F4F4F' : '#4A7C59';
      const onclick = n.isSelf ? 'editSelfProfile()' : `showIdentityEditor('${n.id}')`;
      const labelAbove = n.isSelf ? false : (idx % 2 === 0);
      const ly = labelAbove ? n.y - size - 10 : n.y + size + 14;
      const subLabel = n.isSelf
        ? (self && self.core_traits && self.core_traits.length
            ? `<text x="${n.x}" y="${n.y + size + 6}" text-anchor="middle" font-size="10" fill="#8C8478">${escapeHtml(self.core_traits[0])}</text>`
            : '')
        : `<text x="${n.x}" y="${n.y + size + 6}" text-anchor="middle" font-size="10" fill="#8C8478">激活${n.act}</text>`;
      return `<g onclick="${onclick}" style="cursor:pointer;">
        <circle cx="${n.x}" cy="${n.y}" r="${size}" fill="${color}" fill-opacity="0.12" stroke="${color}" stroke-width="1.6" />
        <circle cx="${n.x}" cy="${n.y}" r="4" fill="${color}" />
        <text x="${n.x}" y="${ly}" text-anchor="middle" font-size="12" fill="#2D2A25" font-weight="600">${escapeHtml(n.name)}</text>
        ${subLabel}
      </g>`;
    }).join('');

    // --- 图例：仅显示实际用到的关系类型 ---
    // --- legend: only relation types in use ---
    const usedTypes = [...new Set(edges.map(e => e.relation_type))];
    const legend = usedTypes.length > 0
      ? `<div style="display:flex;flex-wrap:wrap;gap:10px;justify-content:center;padding:10px 0 2px;font-size:11px;color:var(--text-dim);">${usedTypes.map(t => `<span style="display:inline-flex;align-items:center;gap:4px;"><span style="width:16px;height:2px;background:${RELATION_COLORS[t] || '#7F8C8D'};display:inline-block;"></span>${escapeHtml(t)}</span>`).join('')}</div>`
      : '';

    container.innerHTML = `<svg width="100%" height="${H}" viewBox="0 0 ${W} ${H}" style="display:block;">${edgeParts}${nodeParts}</svg>${legend}`;
  } catch (e) {
    container.innerHTML = `<div style="color:var(--negative);font-size:13px;text-align:center;padding:40px;">关系地图加载失败: ${escapeHtml(e.message)}</div>`;
  }
}

// ========================================
// 自我认知板块：页面最上方，供 AI 记录对自身的了解
// ========================================
function renderSelfProfile(sp) {
  const section = document.getElementById('self-profile-section');
  const body = document.getElementById('self-profile-body');
  // --- 板块始终显示：自我认知固定位于名册最上方，独立于关系地图 ---
  // --- Section always visible: self-understanding sits at the very top, separate from the map ---
  section.style.display = '';
  if (!sp || (!sp.content && !(sp.core_traits && sp.core_traits.length) && !(sp.relation_tags && sp.relation_tags.length))) {
    body.innerHTML = '<div style="color:var(--text-dim);font-size:13px;line-height:1.9;">' +
      'AI 尚未建立自我认知。这是你的身份核心，建议填写：<br>' +
      '· 你的定位与角色（你是谁、在用户生活中承担什么）<br>' +
      '· 性格与沟通偏好（理性 / 温和 / 直接……）<br>' +
      '· 相处原则与边界<br>' +
      '点击右上角「编辑自我认知」开始填写。</div>';
    return;
  }
  const parts = [];
  if (sp.relation_tags && sp.relation_tags.length) {
    parts.push(`<div style="margin-bottom:10px;">${sp.relation_tags.map(t => `<span class="identity-tag">${escapeHtml(t)}</span>`).join('')}</div>`);
  }
  if (sp.core_traits && sp.core_traits.length) {
    parts.push(`<div style="margin-bottom:10px;"><span style="font-size:11px;color:var(--text-dim);margin-right:6px;">性格特征:</span>${sp.core_traits.map(t => `<span style="background:#4A7C5920;color:#4A7C59;padding:3px 10px;border-radius:12px;font-size:12px;margin-right:6px;">${escapeHtml(t)}</span>`).join('')}</div>`);
  }
  if (sp.content) {
    parts.push(`<div style="white-space:pre-wrap;">${escapeHtml(sp.content)}</div>`);
  }
  body.innerHTML = parts.join('');
}

function editSelfProfile() {
  const modal = document.getElementById('self-profile-editor-modal');
  authFetch('/api/roster/self')
    .then(r => r.json())
    .then(data => {
      const sp = data.self_profile || {};
      document.getElementById('self-profile-editor-name').value = sp.name || '自我认知';
      document.getElementById('self-profile-editor-traits').value = (sp.core_traits || []).join(', ');
      document.getElementById('self-profile-editor-relation-tags').value = (sp.relation_tags || []).join(', ');
      document.getElementById('self-profile-editor-content').value = sp.content || '';
      document.getElementById('self-profile-editor-msg').textContent = '';
      modal.style.display = 'flex';
    })
    .catch(e => alert('加载自我认知失败: ' + e.message));
}

function closeSelfProfileEditor() {
  document.getElementById('self-profile-editor-modal').style.display = 'none';
}

async function saveSelfProfile() {
  const msg = document.getElementById('self-profile-editor-msg');
  const data = {
    name: document.getElementById('self-profile-editor-name').value.trim() || '自我认知',
    core_traits: document.getElementById('self-profile-editor-traits').value.split(',').map(s => s.trim()).filter(s => s),
    relation_tags: document.getElementById('self-profile-editor-relation-tags').value.split(',').map(s => s.trim()).filter(s => s),
    content: document.getElementById('self-profile-editor-content').value
  };
  try {
    const resp = await authFetch('/api/roster/self', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data)
    });
    if (!resp) return;
    if (resp.ok) {
      msg.textContent = '保存成功';
      msg.style.color = 'var(--accent)';
      setTimeout(() => {
        closeSelfProfileEditor();
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

// Pattern（模式）编辑/删除：复用通用记忆编辑与删除弹窗
function editPattern(id) {
  editBucket(id);
}

function deletePattern(id) {
  deleteBucket(id);
}

function showIdentityEditor(id) {
  const modal = document.getElementById('identity-editor-modal');
  const title = document.getElementById('identity-editor-title');
  const nameInput = document.getElementById('identity-editor-name');
  const aliasesInput = document.getElementById('identity-editor-aliases');
  const relationTagsInput = document.getElementById('identity-editor-relation-tags');
  const traitsInput = document.getElementById('identity-editor-traits');
  const relationshipsInput = document.getElementById('identity-editor-relationships');
  const contentInput = document.getElementById('identity-editor-content');
  const idInput = document.getElementById('identity-editor-id');
  const basicInfoList = document.getElementById('identity-basic-info-list');
  
  const genderInput = document.getElementById('identity-editor-gender');
  const ageInput = document.getElementById('identity-editor-age');
  const occupationInput = document.getElementById('identity-editor-occupation');
  const interestsInput = document.getElementById('identity-editor-interests');

  // 重置查重提示
  const dupHint = document.getElementById('identity-duplicate-hint');
  if (dupHint) dupHint.style.display = 'none';
  
  if (id) {
    title.textContent = '编辑身份档案';
    authFetch('/api/bucket/' + id)
      .then(r => r.json())
      .then(data => {
        idInput.value = data.id;
        nameInput.value = data.name || data.topic || '';
        aliasesInput.value = data.aliases ? data.aliases.join(', ') : '';
        relationTagsInput.value = data.relation_tags ? data.relation_tags.join(', ') : '';
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
    relationTagsInput.value = '';
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

// 创建新名册时先扫视已有名册：发现同名/别名相同的人物则提示将合并更新
function checkIdentityDuplicate() {
  const id = document.getElementById('identity-editor-id').value;
  const hint = document.getElementById('identity-duplicate-hint');
  if (!hint) return;
  if (id) { hint.style.display = 'none'; return; } // 编辑模式不提示
  const name = document.getElementById('identity-editor-name').value.trim();
  if (!name || !_identityList || _identityList.length === 0) {
    hint.style.display = 'none';
    return;
  }
  const same = _identityList.find(i => {
    const n = (i.name || '').toLowerCase();
    const aliases = (i.aliases || []).map(a => a.toLowerCase());
    const nm = name.toLowerCase();
    return n === nm || aliases.includes(nm);
  });
  if (same) {
    hint.textContent = `已存在同名人物「${same.name}」，保存后将更新其档案而非新建。`;
    hint.style.display = '';
  } else {
    hint.style.display = 'none';
  }
}

// --- 关系类型：建立关系时必须选择具体类型（恋爱/亲情/友情/同事等） ---
// --- relation types: a relation always carries a concrete type ---
const RELATION_TYPES = ['恋人','配偶','家人','父母','子女','兄弟姐妹','亲戚','挚友','朋友','同事','同学','师生','领导','下属','合作伙伴','网友','其他'];

function loadIdentityRelatedList(currentId) {
  const list = document.getElementById('identity-related-list');
  Promise.all([authFetch('/api/identities'), authFetch('/api/roster/self')])
    .then(rs => Promise.all(rs.map(r => r.json())))
    .then(([data, selfData]) => {
      const identities = (data.identities || []).filter(i => i.id !== currentId);
      const sp = selfData.self_profile || null;
      if (identities.length === 0 && !sp) {
        list.innerHTML = '<div style="color:var(--text-dim);font-size:13px;text-align:center;padding:16px;">暂无其他名册，先创建后再来建立关系</div>';
        return;
      }
      // --- 当前身份已有的关系（含类型），用于回显 ---
      // --- existing relations of this identity (with types) for echo ---
      const relMap = {};
      const me = (data.identities || []).find(i => i.id === currentId);
      if (me && me.relations) {
        me.relations.forEach(r => { relMap[r.target_id] = r.relation_type || '朋友'; });
      }

      let html = '';
      // --- 第一行：（我）AI 自身——把人物与 AI 的关系连到地图中心 ---
      // --- first row: AI self — link this person to the map center ---
      if (sp && sp.id) {
        const selfRel = relMap[sp.id];
        const checked = selfRel ? 'checked' : '';
        const sel = RELATION_TYPES.map(t => `<option value="${t}" ${t === (selfRel || '朋友') ? 'selected' : ''}>${t}</option>`).join('');
        html += `
          <div style="display:flex;align-items:center;gap:10px;padding:6px 8px;border-radius:8px;margin-bottom:4px;${selfRel ? 'background:var(--accent-glow);' : ''}" class="identity-rel-row">
            <input type="checkbox" ${checked} onchange="toggleIdentityRelation('${currentId || ''}', '${sp.id}', this)" style="margin:0;cursor:pointer;" />
            <span style="flex:1;font-size:13px;font-weight:500;">（我）AI 自身</span>
            <select onchange="updateRelationType('${currentId || ''}', '${sp.id}', this)" style="font-size:12px;padding:3px 6px;border-radius:6px;border:1px solid var(--border);background:var(--surface);color:var(--text);${selfRel ? '' : 'opacity:0.55;'}" ${selfRel ? '' : 'disabled'}>${sel}</select>
          </div>
        `;
      } else if (currentId) {
        html += '<div style="color:var(--text-dim);font-size:12px;padding:6px 8px;margin-bottom:6px;">先在「自我认知」板块填写 AI 的身份，即可在此建立与 AI 的关系</div>';
      }

      html += identities.map(i => {
        const has = relMap[i.id];
        const checked = has ? 'checked' : '';
        const sel = RELATION_TYPES.map(t => `<option value="${t}" ${t === (has || '朋友') ? 'selected' : ''}>${t}</option>`).join('');
        return `
          <div style="display:flex;align-items:center;gap:10px;padding:6px 8px;border-radius:8px;margin-bottom:4px;${has ? 'background:var(--accent-glow);' : ''}" class="identity-rel-row">
            <input type="checkbox" ${checked} onchange="toggleIdentityRelation('${currentId || ''}', '${i.id}', this)" style="margin:0;cursor:pointer;" />
            <span style="flex:1;font-size:13px;">${escapeHtml(i.name || '未命名')}</span>
            <select onchange="updateRelationType('${currentId || ''}', '${i.id}', this)" style="font-size:12px;padding:3px 6px;border-radius:6px;border:1px solid var(--border);background:var(--surface);color:var(--text);${has ? '' : 'opacity:0.55;'}" ${has ? '' : 'disabled'}>${sel}</select>
          </div>
        `;
      }).join('');
      list.innerHTML = html;
    });
}

async function toggleIdentityRelation(sourceId, targetId, checkbox) {
  if (!sourceId) {
    alert('请先保存名册，再添加关联');
    checkbox.checked = false;
    return;
  }
  const row = checkbox.closest('.identity-rel-row');
  const select = row ? row.querySelector('select') : null;
  const isChecked = checkbox.checked;
  try {
    if (isChecked) {
      const resp = await authFetch('/api/relations/add', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({from_id: sourceId, to_id: targetId, relation_type: select ? select.value : '朋友'})
      });
      const result = await resp.json();
      if (!result.success) {
        checkbox.checked = false;
        alert('建立关系失败，请重试');
        return;
      }
      if (select) { select.disabled = false; select.style.opacity = '1'; }
      if (row) row.style.background = 'var(--accent-glow)';
    } else {
      const resp = await authFetch('/api/relations/remove', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({from_id: sourceId, to_id: targetId})
      });
      const result = await resp.json();
      if (!result.success) {
        checkbox.checked = true;
        alert('移除关系失败，请重试');
        return;
      }
      if (select) { select.disabled = true; select.style.opacity = '0.55'; }
      if (row) row.style.background = '';
    }
  } catch (e) {
    checkbox.checked = !isChecked;
    alert('操作失败: ' + e.message);
  }
}

async function updateRelationType(sourceId, targetId, select) {
  if (!sourceId) return;
  try {
    const resp = await authFetch('/api/relations/add', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({from_id: sourceId, to_id: targetId, relation_type: select.value})
    });
    const result = await resp.json();
    if (!result.success) alert('更新关系类型失败');
  } catch (e) {
    alert('更新关系类型失败: ' + e.message);
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
  const relationTags = document.getElementById('identity-editor-relation-tags').value.split(',').map(s => s.trim()).filter(s => s);
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
  if (relationTags.length > 0) data.relation_tags = relationTags;
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
      let merged = false;
      try {
        const rj = await resp.json();
        merged = rj && rj.merged === true;
      } catch(e) { /* PUT 返回 success 无 merged 字段 */ }
      msg.textContent = merged ? '已更新已有档案' : (id ? '更新成功' : '创建成功');
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
    empty.innerHTML = `<div>生成失败: ${e.message}</div>`;
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
      <div style="width:30px;height:30px;border-radius:50%;background:var(--positive);color:white;display:flex;align-items:center;justify-content:center;font-size:12px;flex-shrink:0;">A</div>
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
          <div style="width:30px;height:30px;border-radius:50%;background:var(--positive);color:white;display:flex;align-items:center;justify-content:center;font-size:12px;flex-shrink:0;">A</div>
          <div style="flex:1;background:white;border-radius:10px;padding:10px;border:1px solid var(--border);font-size:13px;">${escapeHtml(result.response)}</div>
        </div>
      `;
    } else {
      messages.innerHTML += `
        <div style="display:flex;gap:10px;margin-bottom:12px;">
          <div style="width:30px;height:30px;border-radius:50%;background:var(--negative);color:white;display:flex;align-items:center;justify-content:center;font-size:12px;flex-shrink:0;">A</div>
          <div style="flex:1;background:white;border-radius:10px;padding:10px;border:1px solid var(--border);color:var(--negative);font-size:13px;">${escapeHtml(result.error || 'AI回答失败')}</div>
        </div>
      `;
    }
  } catch (e) {
    document.getElementById('global-ai-loading').remove();
    messages.innerHTML += `
      <div style="display:flex;gap:10px;margin-bottom:12px;">
        <div style="width:30px;height:30px;border-radius:50%;background:var(--negative);color:white;display:flex;align-items:center;justify-content:center;font-size:12px;flex-shrink:0;">A</div>
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
        
        html += '<div class="search-result-item" onclick="document.getElementById(\'search-results-panel\').classList.remove(\'open\');showDetail(\'' + r.id + '\')">' +
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

document.addEventListener('click', function(e) {
  var panel = document.getElementById('search-results-panel');
  var searchBar = document.querySelector('.search-bar');
  if (panel && panel.classList.contains('open') && !searchBar.contains(e.target)) {
    panel.classList.remove('open');
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
    { label: '总记忆', value: total, color: 'var(--accent)' },
    { label: '身份', value: identities, color: '#4A7C59' },
    { label: '模式', value: patterns, color: '#6A6A8B' },
    { label: '事件', value: events, color: '#2F4F4F' },
    { label: '感受', value: feels, color: '#8B6A6A' },
    { label: '钉选', value: pinned, color: '#9A7B4F' },
    { label: '已解决', value: resolved, color: '#81C784' },
  ];
  
  container.innerHTML = cards.map(function(c) {
    return '<div class="stat-card" style="border-top:3px solid ' + c.color + ';">' +
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
  summaryHtml += '<div class="mood-summary-item">总标记: ' + totalEmotions + '</div>';
  if (posCount > 0 || negCount > 0) {
    var ratio = posCount + negCount > 0 ? Math.round((posCount / (posCount + negCount)) * 100) : 50;
    summaryHtml += '<div class="mood-summary-item">正向: ' + ratio + '%</div>';
  }
  summaryHtml += '<div class="mood-summary-item">' + new Date().toLocaleDateString('zh-CN') + '</div>';
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
    var preview = esc((b.content || '').substring(0, 100));
    var bucketType = b.type || (b.metadata && b.metadata.type) || 'event';
    var typeColors = {
      'identity': '#4A7C59', 'pattern': '#9A7B4F', 'feel': '#8B4A4A',
      'event': '#2F4F4F', 'archive': '#6A6A8B', 'permanent': '#4A7C59', 'dynamic': '#2F4F4F'
    };
    var typeLabels = {
      'identity': '身份', 'pattern': '模式', 'feel': '感受',
      'event': '事件', 'archive': '归档', 'permanent': '永久', 'dynamic': '动态'
    };
    var typeColor = typeColors[bucketType] || '#888';
    var typeLabel = typeLabels[bucketType] || bucketType;
    
    html += '<div class="timeline-entry" onclick="showDetail(\'' + b.id + '\')">' +
      '<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">' +
      '<div class="tl-time">' + formatTimeAgo(timeStr) + '</div>' +
      '<span style="font-size:10px;padding:1px 6px;border-radius:4px;background:rgba(' + hexToRgb(typeColor) + ',0.1);color:' + typeColor + ';">' + typeLabel + '</span>' +
      '</div>' +
      '<div class="tl-name">' + name + '</div>' +
      (preview ? '<div class="tl-preview">' + preview + '…</div>' : '') +
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

