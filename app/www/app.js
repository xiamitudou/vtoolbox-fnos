(function () {
  'use strict';

  // 接口与静态资源的基准前缀。
  // 服务端会在首页 <head> 里注入 <base href>：
  //   直连端口  -> http://<主机>:5230/
  //   统一网关  -> http(s)://<域名>/app/vtoolbox/
  // 这里直接以 <base> 为基准，保证两种访问方式下资源与接口都落在同一前缀内。
  var API = (function () {
    var href = document.baseURI || window.location.href;
    var cut = href.indexOf('?');
    if (cut >= 0) { href = href.slice(0, cut); }
    cut = href.indexOf('#');
    if (cut >= 0) { href = href.slice(0, cut); }
    if (href.charAt(href.length - 1) !== '/') { href += '/'; }
    return href;
  })();

  function apiUrl(path) {
    return API + String(path || '').replace(/^\/+/, '');
  }
  var VIDEO_DEFAULT = ['mp4', 'mkv', 'mov', 'm4v', 'avi', 'ts', 'm2ts', 'webm', 'flv', 'wmv', 'mpg', 'mpeg', 'rmvb', '3gp'];

  var state = {
    cfg: {},
    roots: [],
    defaultOutput: '',
    extensions: VIDEO_DEFAULT,
    cwd: '',
    listMode: 'browse',
    dirs: [],
    videos: [],
    scanned: [],
    filter: '',
    picked: new Map(),
    logsOpen: {},
    info: {},
    polling: null,
    mode: 'trim',
    sortKey: 'name',
    sortDir: 'asc',
    outputTouched: false,
    coverGroups: [],
    coverInfo: null,
    coverBusy: false,
    mergeGroups: null,
    mergeBusy: false
  };

  var LAST_ROOT_KEY = 'vtoolbox.lastRoot';

  // ---------------------------------------------------------------- 工具

  function $(id) { return document.getElementById(id); }

  function esc(text) {
    return String(text == null ? '' : text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function toast(message, isError) {
    var el = $('toast');
    el.textContent = message;
    el.className = 'toast show' + (isError ? ' err' : '');
    clearTimeout(el._timer);
    el._timer = setTimeout(function () { el.className = 'toast'; }, 3600);
  }

  function api(path, options) {
    options = options || {};
    var init = { method: options.method || 'GET', headers: {} };
    if (options.body) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(options.body);
    }
    return fetch(apiUrl(path), init).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok || data.error) {
          throw new Error(data.error || ('请求失败（HTTP ' + response.status + '）'));
        }
        return data;
      });
    });
  }

  function parseDuration(value) {
    var text = String(value || '').trim();
    if (!text) { return 0; }
    if (text.indexOf(':') >= 0) {
      var parts = text.split(':');
      var total = 0;
      for (var i = 0; i < parts.length; i++) {
        total = total * 60 + (parseFloat(parts[i]) || 0);
      }
      return total;
    }
    return parseFloat(text) || 0;
  }

  function fmtSeconds(seconds) {
    seconds = Math.max(0, Number(seconds) || 0);
    var h = Math.floor(seconds / 3600);
    var m = Math.floor((seconds % 3600) / 60);
    var s = Math.round(seconds % 60);
    if (h > 0) { return h + ':' + pad(m) + ':' + pad(s); }
    return m + ':' + pad(s);
  }

  function pad(n) { return (n < 10 ? '0' : '') + n; }

  function baseName(path) { return String(path || '').split('/').pop(); }

  // ---------------------------------------------------------------- 初始化

  function loadState() {
    return api('/api/state').then(function (data) {
      state.info = data;
      state.cfg = data.config || {};
      state.roots = data.roots || [];
      state.defaultOutput = data.default_output_dir || '';
      state.extensions = data.extensions || VIDEO_DEFAULT;

      var ff = data.ffmpeg || {};
      var badge = $('ffmpegBadge');
      if (ff.available) {
        badge.textContent = 'ffmpeg ' + (ff.version || '已就绪') + (ff.builtin ? '' : '（系统）');
        badge.className = ff.builtin ? 'badge' : 'badge warn';
        badge.title = ff.path || '';
      } else {
        badge.textContent = 'ffmpeg 不可用';
        badge.className = 'badge err';
        badge.title = '请确认应用包内 bin/ffmpeg 存在，或在应用设置中指定 ffmpeg 路径';
      }
      $('subtitle').textContent = '批量删除片头/片尾 · 批量合并 · 原片不被修改';

      applyConfigToForm();
      renderEnv();
      renderRoots();
      renderCrumbs();
      renderTable();

      var output = $('outputDir');
      if (!output.value) { output.value = state.defaultOutput || (state.roots[0] || ''); }

      if (state.roots.length) {
        var remembered = null;
        try { remembered = window.localStorage.getItem(LAST_ROOT_KEY); } catch (e) { remembered = null; }
        var start = state.roots.indexOf(remembered) >= 0 ? remembered : state.roots[0];
        browse(start);
      } else {
        $('browseWrap').innerHTML = '<div class="empty">尚未授权任何目录。<br>请在飞牛「应用设置 → 授权目录」中把视频所在目录授予本应用，保存后重新打开本页。</div>';
      }
      pollJobs(true);
      return data;
    }).catch(function (err) {
      toast('加载失败：' + err.message, true);
      $('browseWrap').innerHTML = '<div class="empty">加载失败：' + esc(err.message) + '</div>';
    });
  }

  function applyConfigToForm() {
    $('cfgTrimMode').value = state.cfg.trim_mode || 'accurate';
    $('cfgConcurrency').value = String(state.cfg.concurrency || 1);
    $('cfgOnExists').value = state.cfg.on_exists || 'rename';
    $('cfgCrf').value = state.cfg.crf || 20;
    $('cfgPreset').value = state.cfg.preset || 'veryfast';
    $('cfgHwaccel').value = state.cfg.hwaccel || 'off';
    var outMode = $('cfgOutputMode');
    if (outMode) { outMode.value = state.cfg.output_mode || 'default'; }
    $('trimMode').value = state.cfg.trim_mode || 'accurate';
    renderHwHint();
  }

  var SUFFIX_BY_TARGET = { head: '_已删片头', tail: '_已删片尾', both: '_已删片头尾' };

  // 按「删除范围」显示对应的时长输入（只删片头时完全保持旧界面）
  function renderTrimTarget() {
    var target = $('trimTarget').value;
    var showHead = target === 'head' || target === 'both';
    var showTail = target === 'tail' || target === 'both';
    [['headField', showHead], ['headChips', showHead], ['headHint', showHead],
     ['tailField', showTail], ['tailChips', showTail], ['tailHint', showTail]]
      .forEach(function (pair) {
        var el = $(pair[0]);
        if (el) { el.classList.toggle('hidden', !pair[1]); }
      });
    var suffix = $('nameSuffix');
    var presets = Object.keys(SUFFIX_BY_TARGET).map(function (key) {
      return SUFFIX_BY_TARGET[key];
    });
    // 后缀只在用户没有自定义过时跟随删除范围变化
    if (suffix && (!suffix.value || presets.indexOf(suffix.value) >= 0)) {
      suffix.value = SUFFIX_BY_TARGET[target];
    }
  }

  // ---------------------------------------------------------------- 合并后处理源视频

  function mergedDirName() {
    var app = state.info.app || {};
    return app.merged_dir_name || '已合并视频';
  }

  function mergePostAction() {
    var sel = $('mergePostAction');
    return sel ? sel.value : 'keep';
  }

  var MERGE_POST_TEXT = {
    keep: function () { return '合并完成后不改动源视频。'; },
    move: function () {
      return '合并完成后，把参与合并的源视频移动到它们各自所在目录下的「' +
        mergedDirName() + '」子文件夹里归档；同名文件会自动改名，不会覆盖。';
    },
    delete: function () {
      return '合并完成后永久删除参与合并的源视频，删除后无法恢复。';
    }
  };

  function renderMergePost() {
    var sel = $('mergePostAction');
    var hint = $('mergePostHint');
    if (!sel || !hint) { return; }
    var value = sel.value;
    var build = MERGE_POST_TEXT[value] || MERGE_POST_TEXT.keep;
    var text = build();
    if (value !== 'keep') {
      // 单独成组的文件根本不会产出成品，因此不在处理范围内 —— 这是用户明确要求的行为
      text += '「直接拼接」中单独成组（参数与其他文件不同、只有 1 个文件）的视频不参与合并，也不会被处理。';
    }
    hint.textContent = text;
    hint.classList.toggle('danger', value === 'delete');
  }

  function renderHwHint() {
    var hw = state.info.hwaccel || {};
    var el = $('hwHint');
    if (!el) { return; }
    if (hw.available) {
      el.textContent = '已检测到可用核显：' + (hw.label || '') +
        (hw.device ? '（' + hw.device + '）' : '') +
        '。开启后仅作用于「标准合并」与「整片重编码」，精准切割仍用软件编码；硬件失败会自动回退。';
    } else {
      el.textContent = '当前不可用：' + (hw.reason || '未检测到核显') +
        '。包内自带静态 ffmpeg 不含核显编码器，硬件加速会改用系统 ffmpeg，并需要应用具备 /dev/dri 权限。';
    }
  }

  function renderEnv() {
    var ff = state.info.ffmpeg || {};
    var hw = state.info.hwaccel || {};
    var rows = [
      ['应用版本', '视频工具箱 ' + ((state.info.app || {}).version || '-')],
      ['ffmpeg', ff.available ? (ff.version || '已就绪') + '（' + (ff.source || '内置') + '）' : '不可用'],
      ['ffmpeg 路径', ff.path || '-'],
      ['核显加速', hw.available ? ((hw.label || '可用') + (hw.active ? ' · 已启用' : ' · 未启用')) : ('不可用 · ' + (hw.reason || ''))],
      ['授权目录', (state.roots || []).join('\n') || '（无）'],
      ['默认输出目录', state.defaultOutput || '（请在授权目录中选择）'],
      ['临时目录', state.info.tmp_dir || '-']
    ];
    $('envList').innerHTML = rows.map(function (row) {
      return '<li><span>' + esc(row[0]) + '</span><span>' + esc(row[1]) + '</span></li>';
    }).join('');
  }

  // ---------------------------------------------------------------- 授权目录切换

  function rootLabel(path) {
    var parts = String(path || '').split('/').filter(Boolean);
    return parts.length ? parts[parts.length - 1] : path;
  }

  function activeRoot() {
    var best = '';
    state.roots.forEach(function (root) {
      if (state.cwd === root || state.cwd.indexOf(root + '/') === 0) {
        if (root.length > best.length) { best = root; }
      }
    });
    return best;
  }

  function renderRoots() {
    var wrap = $('rootsBar');
    if (!wrap) { return; }
    if (state.roots.length <= 1) {
      wrap.innerHTML = '';
      return;
    }
    var current = activeRoot();
    var html = '<span class="roots-label">授权目录</span>';
    state.roots.forEach(function (root) {
      html += '<button class="root-chip' + (root === current ? ' active' : '') + '"' +
        ' data-root="' + esc(root) + '" title="' + esc(root) + '">' +
        '<span class="root-flag">' + (root === current ? '●' : '○') + '</span>' +
        '<span class="root-name">' + esc(rootLabel(root)) + '</span>' +
        '</button>';
    });
    wrap.innerHTML = html;
  }

  // ---------------------------------------------------------------- 目录浏览

  function browse(path) {
    if (!path) { toast('请选择一个授权目录', true); return Promise.resolve(); }
    return api('/api/browse?path=' + encodeURIComponent(path)).then(function (data) {
      state.cwd = data.path || '';
      state.dirs = data.dirs || [];
      state.videos = data.videos || [];
      if (data.roots && data.roots.length) { state.roots = data.roots; }
      state.listMode = 'browse';
      state.scanned = [];
      state.coverGroups = [];
      state.coverInfo = null;
      rememberRoot();
      renderRoots();
      renderCrumbs();
      renderTable();
      applyOutputMode();
      updateUpButton();
    }).catch(function (err) {
      toast(err.message, true);
    });
  }

  // 输出位置默认值：current = 跟随当前浏览目录；default = 用应用默认输出目录。
  // 用户在本次会话里手动点过「用当前目录 / 用默认目录」之后就不再自动覆盖。
  function applyOutputMode() {
    var input = $('outputDir');
    if (!input || state.outputTouched) { return; }
    if ((state.cfg.output_mode || 'default') === 'current') {
      input.value = state.cwd || state.defaultOutput || '';
    } else {
      input.value = state.defaultOutput || (state.roots[0] || '');
    }
  }

  function rememberRoot() {
    var root = activeRoot();
    if (!root) { return; }
    try { window.localStorage.setItem(LAST_ROOT_KEY, root); } catch (e) { /* 忽略隐私模式 */ }
  }

  function updateUpButton() {
    var button = $('btnUp');
    if (!button) { return; }
    if (state.listMode === 'browse') {
      button.disabled = !state.cwd || !activeRoot() || state.cwd === activeRoot();
    } else {
      button.disabled = false;
    }
  }

  function scanCurrent() {
    if (!state.cwd) { toast('请先进入一个目录', true); return; }
    $('browseWrap').innerHTML = '<div class="empty">正在递归扫描…</div>';
    api('/api/scan', { method: 'POST', body: { root: state.cwd, recursive: true } })
      .then(function (data) {
        state.scanned = data.files || [];
        state.listMode = 'scan';
        state.coverGroups = [];
        state.coverInfo = null;
        renderRoots();
        renderCrumbs();
        renderTable();
        updateUpButton();
        toast('扫描到 ' + state.scanned.length + ' 个视频');
      })
      .catch(function (err) {
        toast(err.message, true);
        renderTable();
      });
  }

  function renderCrumbs() {
    var wrap = $('crumbs');
    if (state.listMode === 'scan') {
      wrap.innerHTML = '<button data-action="back">← 返回目录</button><span>·</span>' +
        '<span>递归扫描结果（' + state.scanned.length + ' 个视频）</span>';
      return;
    }
    if (state.listMode === 'cover') {
      var info = state.coverInfo || {};
      wrap.innerHTML = '<button data-action="back">← 返回目录</button><span>·</span>' +
        '<span>按封面分组：' + (info.total || 0) + ' 个视频 → ' + (info.group_count || 0) +
        ' 组，其中封面相同的 ' + (info.duplicate_files || 0) + ' 个' +
        (info.elapsed != null ? '（耗时 ' + info.elapsed + ' 秒' + (info.cached ? '，缓存命中 ' + info.cached : '') + '）' : '') +
        '</span>';
      return;
    }
    if (!state.cwd) {
      wrap.innerHTML = '<span>请选择授权目录</span>';
      return;
    }
    var html = '';
    var matchedRoot = activeRoot();
    if (matchedRoot) {
      var rest = state.cwd.slice(matchedRoot.length).split('/').filter(Boolean);
      html += '<button data-path="' + esc(matchedRoot) + '">' + esc(matchedRoot) + '</button>';
      var acc = matchedRoot;
      rest.forEach(function (part) {
        acc += '/' + part;
        html += '<span>/</span><button data-path="' + esc(acc) + '">' + esc(part) + '</button>';
      });
    } else {
      html += '<span>' + esc(state.cwd) + '</span>';
    }
    wrap.innerHTML = html;
  }

  function renderTable() {
    var wrap = $('browseWrap');
    var coverMode = state.listMode === 'cover';
    var dupLabel = $('onlyDupLabel');
    if (dupLabel) { dupLabel.classList.toggle('hidden', !coverMode); }
    if (coverMode) {
      renderCoverGroups(wrap);
      return;
    }
    if (state.listMode === 'scan') {
      wrap.innerHTML = tableHtml(sortRows(state.scanned.map(function (file) {
        return {
          path: file.path,
          name: file.name,
          sub: file.dir || '',
          size_text: file.size_text,
          size: file.size,
          isDir: false
        };
      })), true);
      bindTable();
      return;
    }
    var rows = state.dirs.map(function (dir) {
      return { path: dir.path, name: dir.name, isDir: true, sub: '目录' };
    }).concat(state.videos.map(function (video) {
      return { path: video.path, name: video.name, isDir: false,
               size_text: video.size_text, size: video.size };
    }));
    wrap.innerHTML = tableHtml(sortRows(rows), false);
    bindTable();
  }

  // ---------------------------------------------------------------- 排序

  function sortValue(row, key) {
    if (key === 'duration') {
      var info = state.picked.get(row.path);
      var value = info ? info.duration : 0;
      return (typeof value === 'number' && value > 0) ? value : null;
    }
    return (typeof row.size === 'number' && row.size > 0) ? row.size : null;
  }

  function sortRows(rows) {
    var key = state.sortKey;
    var flip = state.sortDir === 'desc' ? -1 : 1;
    function cmp(a, b) {
      if (key === 'name') {
        return flip * String(a.name).localeCompare(String(b.name), 'zh-CN',
                   { numeric: true, sensitivity: 'base' });
      }
      var av = sortValue(a, key);
      var bv = sortValue(b, key);
      // 时长尚未读出（未勾选）的项无论升降序都排在最后，免得挤在最前面
      if (av === null && bv === null) { return 0; }
      if (av === null) { return 1; }
      if (bv === null) { return -1; }
      if (av === bv) { return 0; }
      return flip * (av < bv ? -1 : 1);
    }
    var dirs = rows.filter(function (row) { return row.isDir; });
    var files = rows.filter(function (row) { return !row.isDir; });
    dirs.sort(cmp);
    files.sort(cmp);
    return dirs.concat(files);      // 目录始终置顶
  }

  function sortArrow(key) {
    if (state.sortKey !== key) { return ''; }
    return state.sortDir === 'desc' ? ' ▾' : ' ▴';
  }

  function toggleSort(key) {
    if (state.sortKey === key) {
      state.sortDir = state.sortDir === 'desc' ? 'asc' : 'desc';
    } else {
      state.sortKey = key;
      state.sortDir = 'asc';
    }
    renderTable();
  }

  function tableHtml(rows, withSub) {
    var filter = state.filter.toLowerCase();
    var shown = rows.filter(function (row) {
      if (!filter) { return true; }
      return row.name.toLowerCase().indexOf(filter) >= 0;
    });
    if (!shown.length) {
      return '<div class="empty">' + (rows.length ? '没有匹配的项目' : '这个目录里没有视频') + '</div>';
    }
    var html = '<table><thead><tr><th style="width:34px"></th>' +
      '<th class="sortable" data-sort="name" title="点击按名称排序">名称' +
        sortArrow('name') + '</th>' +
      (withSub ? '<th>所在目录</th>' : '') +
      '<th class="num sortable" data-sort="duration" id="thDuration" title="点击按时长排序">时长' +
        sortArrow('duration') + '</th>' +
      '<th class="num sortable" data-sort="size" title="点击按大小排序">大小' +
        sortArrow('size') + '</th>' +
      '</tr></thead><tbody>';
    shown.forEach(function (row) {
      var picked = state.picked.has(row.path);
      var info = state.picked.get(row.path) || {};
      html += '<tr class="' + (row.isDir ? 'dir' : 'file') + '" data-path="' + esc(row.path) + '">' +
        '<td><input type="checkbox" data-pick="' + esc(row.path) + '"' + (picked ? ' checked' : '') + (row.isDir ? ' disabled' : '') + '></td>' +
        '<td class="name" title="' + esc(row.name) + '">' + (row.isDir ? '📁 ' : '') + esc(row.name) + '</td>' +
        (withSub ? '<td class="num">' + esc(row.sub || '') + '</td>' : '') +
        '<td class="num">' + (info.duration_text || '—') + '</td>' +
        '<td class="num">' + esc(row.size_text || '') + '</td>' +
        '</tr>';
    });
    html += '</tbody></table>';
    return html;
  }

  function bindTable() {
    var wrap = $('browseWrap');
    wrap.querySelectorAll('th[data-sort]').forEach(function (th) {
      th.addEventListener('click', function () {
        toggleSort(th.getAttribute('data-sort'));
      });
    });
    wrap.querySelectorAll('tr.dir').forEach(function (tr) {
      tr.addEventListener('click', function (event) {
        if (event.target.tagName === 'INPUT') { return; }
        browse(tr.getAttribute('data-path'));
      });
    });
    wrap.querySelectorAll('input[data-pick]').forEach(function (box) {
      box.addEventListener('change', function () {
        var path = box.getAttribute('data-pick');
        if (box.checked) {
          pick(path);
        } else {
          state.picked.delete(path);
          renderPicked();
        }
      });
    });
  }

  // ---------------------------------------------------------------- 封面分组

  function thumbUrl(path) {
    return apiUrl('api/thumb') + '?path=' + encodeURIComponent(path);
  }

  function currentVideoPaths() {
    var rows = state.listMode === 'scan' ? state.scanned : state.videos;
    var filter = state.filter.toLowerCase();
    return rows.filter(function (row) {
      if (!filter) { return true; }
      return row.name.toLowerCase().indexOf(filter) >= 0;
    }).map(function (row) { return row.path; });
  }

  function runCoverGroup() {
    if (state.listMode === 'cover') {
      state.listMode = state.scanned.length ? 'scan' : 'browse';
      renderRoots();
      renderCrumbs();
      renderTable();
      updateUpButton();
      return;
    }
    var paths = currentVideoPaths();
    if (!paths.length) { toast('当前列表没有可分析的视频', true); return; }
    if (paths.length > 600) { paths = paths.slice(0, 600); }
    if (state.coverBusy) { return; }
    state.coverBusy = true;
    $('browseWrap').innerHTML = '<div class="empty">正在提取每个视频的封面并比对…<br>首次分析较慢，之后会命中缓存。</div>';
    var threshold = state.cfg.cover_threshold;
    if (threshold == null) { threshold = 6; }
    api('/api/analyze', {
      method: 'POST',
      body: { by: 'cover', paths: paths, threshold: threshold }
    }).then(function (data) {
      state.coverGroups = data.groups || [];
      state.coverInfo = data;
      state.listMode = 'cover';
      renderRoots();
      renderCrumbs();
      renderTable();
      updateUpButton();
      var dup = data.duplicate_groups || 0;
      toast(dup ? ('发现 ' + dup + ' 组封面相同的视频') : '没有发现封面相同的视频');
    }).catch(function (err) {
      toast('分析失败：' + err.message, true);
      renderTable();
    }).then(function () {
      state.coverBusy = false;
    });
  }

  function renderCoverGroups(wrap) {
    var groups = state.coverGroups || [];
    if (!groups.length) {
      wrap.innerHTML = '<div class="empty">没有可显示的分组</div>';
      return;
    }
    var onlyDup = !!$('onlyDup').checked;
    var shown = groups.filter(function (group) {
      return !onlyDup || group.count > 1;
    });
    if (!shown.length) {
      wrap.innerHTML = '<div class="empty">没有封面相同的分组（当前只显示重复组）</div>';
      return;
    }
    var html = '<div class="cover-wrap">';
    shown.forEach(function (group) {
      var isDup = group.count > 1;
      var pickedCount = group.items.filter(function (item) {
        return state.picked.has(item.path);
      }).length;
      html += '<div class="group-card' + (isDup ? ' dup' : '') + '" data-group="' + group.index + '">' +
        '<div class="group-head">' +
        '<img class="group-thumb" loading="lazy" src="' + esc(thumbUrl(group.thumb)) + '" alt="">' +
        '<div class="group-meta">' +
        '<div class="group-title">组 ' + group.index + ' · ' + group.count + ' 个视频' +
        (isDup ? ' <span class="badge warn">封面相同</span>' : '') +
        (pickedCount ? ' <span class="badge ok">已选 ' + pickedCount + '</span>' : '') +
        '</div>' +
        '<div class="group-sub">' + (isDup ? '封面一致，通常同源或同系列' : '封面各不相同') + '</div>' +
        '</div>' +
        '<div class="group-actions">' +
        '<button class="btn ghost small" data-group-pick="' + group.index + '">' + (pickedCount === group.count && group.count > 1 ? '取消该组' : '选中该组') + '</button>' +
        '</div>' +
        '</div>' +
        '<ul class="group-items">' +
        group.items.map(function (item) {
          var picked = state.picked.has(item.path);
          return '<li>' +
            '<input type="checkbox" data-pick="' + esc(item.path) + '"' + (picked ? ' checked' : '') + '>' +
            '<span class="gi-name" title="' + esc(item.path) + '">' + esc(item.name) + '</span>' +
            '<span class="gi-tag">' + esc(item.hash || '—') + '</span>' +
            '</li>';
        }).join('') +
        '</ul></div>';
    });
    html += '</div>';
    wrap.innerHTML = html;

    wrap.querySelectorAll('input[data-pick]').forEach(function (box) {
      box.addEventListener('change', function () {
        var path = box.getAttribute('data-pick');
        if (box.checked) { pick(path); } else { state.picked.delete(path); renderPicked(); }
        renderTable();
      });
    });
    wrap.querySelectorAll('button[data-group-pick]').forEach(function (button) {
      button.addEventListener('click', function () {
        var index = parseInt(button.getAttribute('data-group-pick'), 10);
        var group = (state.coverGroups || []).filter(function (g) { return g.index === index; })[0];
        if (!group) { return; }
        var allPicked = group.items.every(function (item) { return state.picked.has(item.path); });
        group.items.forEach(function (item) {
          if (allPicked) { state.picked.delete(item.path); } else { pick(item.path); }
        });
        renderTable();
        renderPicked();
        toast(allPicked ? ('已取消该组 ' + group.items.length + ' 个文件')
                        : ('已选中该组 ' + group.items.length + ' 个文件'));
      });
    });
    wrap.querySelectorAll('img.group-thumb').forEach(function (img) {
      img.addEventListener('error', function () {
        img.style.visibility = 'hidden';
      });
    });
  }

  // ---------------------------------------------------------------- 合并分组方式

  function mergeGroup() {
    var sel = $('mergeGroup');
    return sel ? sel.value : 'params';
  }

  // 界面上的容差单位是「百分比」（0~100，5 表示 5%），后端用 0~1 的比值。
  function orientationRatio() {
    var input = $('orientationRatio');
    var app = state.info.app || {};
    var defPct = Math.round((app.orientation_ratio_default || 0.05) * 100);
    var minPct = Math.round((app.orientation_ratio_min || 0) * 100);
    var maxPct = Math.round((app.orientation_ratio_max || 1) * 100);
    var pct = input ? parseFloat(input.value) : defPct;
    if (!isFinite(pct)) { pct = defPct; }
    pct = Math.max(minPct, Math.min(maxPct, pct));
    return pct / 100;
  }

  var MERGE_GROUP_HINT = {
    params: function () {
      return '参数（编码/分辨率/帧率/音轨）完全一致的文件才会合并到同一个视频里；' +
        '参数不同的文件各自成为一组。';
    },
    resolution: function () {
      return '画面像素尺寸完全相同的文件归为一组、各自合并为一个视频；' +
        '不同分辨率的视频互不混合。适合把同一批不同清晰度的素材分开成片。';
    },
    orientation: function () {
      var pct = Math.round(orientationRatio() * 100);
      return '横屏视频只和横屏合并，竖屏视频只和竖屏合并，比例接近方形（宽高比在 1.00±' +
        pct + '% 以内）的单独成一组。容差可调：数值越大，越容易被当成方形单独分组。';
    }
  };

  function renderMergeGroup() {
    var group = mergeGroup();
    var field = $('orientationRatioField');
    var hint = $('mergeGroupHint');
    if (field) { field.classList.toggle('hidden', group !== 'orientation'); }
    if (hint) {
      hint.textContent = (MERGE_GROUP_HINT[group] || MERGE_GROUP_HINT.params)();
    }
  }

  // ---------------------------------------------------------------- 参数分组预览

  function analyzeMergeGroups() {
    var files = currentFiles();
    if (files.length < 2) { toast('至少选择两个视频再分析', true); return; }
    if (state.mergeBusy) { return; }
    state.mergeBusy = true;
    var box = $('mergeGroupBox');
    box.classList.remove('hidden');
    $('mergeGroupList').innerHTML = '<div class="empty-small">正在分析各文件的编码参数…</div>';
    var payload = { by: mergeGroup(), paths: files };
    if (mergeGroup() === 'orientation') { payload.orientation_ratio = orientationRatio(); }
    api('/api/analyze', { method: 'POST', body: payload })
      .then(function (data) {
        state.mergeGroups = data;
        renderMergeGroups();
      })
      .catch(function (err) {
        $('mergeGroupList').innerHTML = '<div class="gp-item gp-warn">分析失败：' + esc(err.message) + '</div>';
      })
      .then(function () {
        state.mergeBusy = false;
      });
  }

  function renderMergeGroups() {
    var data = state.mergeGroups;
    var list = $('mergeGroupList');
    if (!data) { list.innerHTML = ''; return; }
    var titles = { params: '参数分组', resolution: '分辨率分组', orientation: '横竖屏分组' };
    $('mergeGroupTitle').textContent = (titles[data.by] || '参数分组') +
      ' · ' + data.group_count + ' 组';
    var html = '';
    if (data.compatible) {
      html += '<div class="gp-item gp-label">所有文件参数一致，合并为一个视频即可。</div>';
    } else {
      html += '<div class="gp-item gp-label gp-warn">参数不一致：' + esc(data.reason || '') + '</div>';
      html += '<div class="gp-item gp-sub">下面每组会各自合并为一个视频，共输出 ' +
        data.mergeable_count + ' 个视频；只有 1 个文件的组会被跳过。</div>';
    }
    (data.groups || []).forEach(function (group) {
      var note = group.mergeable ? '' : ' · 仅 1 个文件，无法合并';
      if (!group.mergeable && mergePostAction() !== 'keep') {
        note += '，也不会被' + (mergePostAction() === 'move' ? '移动' : '删除');
      }
      html += '<div class="gp-item">' +
        '<div class="gp-label">第 ' + group.index + ' 组 · ' + group.count + ' 个 · 合计 ' + esc(group.duration_text) + '</div>' +
        '<div class="gp-sub">' + esc(group.label) + note + '</div>' +
        '</div>';
    });
    (data.errors || []).forEach(function (err) {
      html += '<div class="gp-item gp-warn">' + esc(baseName(err.path)) + '：' + esc(err.error) + '</div>';
    });
    list.innerHTML = html;
  }

  function clearMergeGroups() {
    state.mergeGroups = null;
    $('mergeGroupBox').classList.add('hidden');
    $('mergeGroupList').innerHTML = '';
  }

  // ---------------------------------------------------------------- 选择与排序

  function pick(path) {
    if (!state.picked.has(path)) {
      state.picked.set(path, { path: path, name: baseName(path) });
    }
    renderPicked();
    probeMissing();
  }

  // 单批探测的文件数：宁小勿大。飞牛网关（nginx）默认 60 秒读超时，
  // 一次把几百个文件丢给后端串行 ffprobe 必然超时并返回 502，
  // 所以改成「小批 + 串行 + 失败拆半重试」，后端本身也是并发处理的。
  var PROBE_BATCH = 10;
  var PROBE_MAX_SPLIT = 2;
  var probeQueue = [];
  var probeRunning = false;

  function probeMissing() {
    var missing = [];
    state.picked.forEach(function (item) {
      if (!item.probed && !item.probing) {
        item.probing = true;
        missing.push(item.path);
      }
    });
    if (!missing.length) { return; }
    probeQueue = probeQueue.concat(missing);
    pumpProbeQueue();
  }

  function pumpProbeQueue() {
    if (probeRunning) { return; }
    if (!probeQueue.length) {
      renderTable();
      renderPicked();
      return;
    }
    probeRunning = true;
    var batch = probeQueue.splice(0, PROBE_BATCH);
    probeBatch(batch, 0).then(function () {
      probeRunning = false;
      renderTable();
      renderPicked();
      pumpProbeQueue();
    });
  }

  function probeBatch(paths, depth) {
    return api('/api/probe', { method: 'POST', body: { paths: paths } })
      .then(applyProbeResult)
      .catch(function (err) {
        if (paths.length > 2 && depth < PROBE_MAX_SPLIT) {
          var mid = Math.ceil(paths.length / 2);
          return probeBatch(paths.slice(0, mid), depth + 1).then(function () {
            return probeBatch(paths.slice(mid), depth + 1);
          });
        }
        paths.forEach(function (path) {
          var item = state.picked.get(path);
          if (item) { item.probing = false; }
        });
        toast('读取视频信息失败：' + err.message, true);
      });
  }

  function applyProbeResult(data) {
    (data.items || []).forEach(function (info) {
      var item = state.picked.get(info.path);
      if (item) {
        item.probed = true;
        item.probing = false;
        item.error = null;
        item.duration = info.duration;
        item.duration_text = info.duration_text;
        item.width = info.width;
        item.height = info.height;
        item.vcodec = info.vcodec;
        item.ext = info.ext;
      }
    });
    (data.errors || []).forEach(function (err) {
      var item = state.picked.get(err.path);
      if (item) {
        item.probing = false;
        item.error = err.error;
      }
    });
  }

  function renderPicked() {
    var items = [];
    state.picked.forEach(function (item) { items.push(item); });
    var total = items.reduce(function (sum, item) { return sum + (item.duration || 0); }, 0);
    var text = '已选 ' + items.length + ' 个文件';
    if (total > 0) { text += ' · 合计时长 ' + fmtSeconds(total); }
    $('pickedSummary').textContent = text;
    renderOrder(items);
  }

  function renderOrder(items) {
    if (state.mode !== 'merge') { return; }
    var list = $('orderList');
    if (!items.length) {
      list.innerHTML = '<li class="empty-small">从左侧勾选两个以上视频后在此排序</li>';
      return;
    }
    list.innerHTML = items.map(function (item) {
      return '<li>' + esc(item.name) + (item.duration_text ? ' <span style="color:var(--text-3)">' + esc(item.duration_text) + '</span>' : '') + '</li>';
    }).join('');
  }

  // ---------------------------------------------------------------- 提交任务

  function currentFiles() {
    var files = [];
    state.picked.forEach(function (item) { files.push(item.path); });
    return files;
  }

  function startJob() {
    var files = currentFiles();
    if (!files.length) { toast('请先勾选视频文件', true); return; }
    var outputDir = $('outputDir').value.trim();
    if (!outputDir) { toast('请填写输出目录', true); return; }

    var options = {
      output_dir: outputDir,
      output_ext: $('outputExt').value,
      on_exists: $('onExists').value || null
    };
    var kind = state.mode;

    if (kind === 'trim') {
      var target = $('trimTarget').value;
      options.trim_target = target;
      if (target === 'head' || target === 'both') {
        var head = parseDuration($('headInput').value);
        if (head <= 0) { toast('片头时长必须大于 0', true); return; }
        options.head_seconds = head;
      }
      if (target === 'tail' || target === 'both') {
        var tail = parseDuration($('tailInput').value);
        if (tail <= 0) { toast('片尾时长必须大于 0', true); return; }
        options.tail_seconds = tail;
      }
      options.mode = $('trimMode').value;
      options.name_suffix = $('nameSuffix').value;
    } else {
      if (files.length < 2) { toast('合并至少需要两个文件', true); return; }
      options.mode = $('mergeMode').value;
      options.merge_group = mergeGroup();
      if (options.merge_group === 'orientation') {
        options.orientation_ratio = orientationRatio();
      }
      options.output_name = $('mergeName').value.trim();
      options.post_action = $('mergePostAction').value;
      if (options.merge_group === 'params' && options.mode === 'copy' &&
          state.mergeGroups && !state.mergeGroups.compatible) {
        toast('参数不一致，将按参数分成 ' + state.mergeGroups.mergeable_count + ' 组分别合并');
      }
      if (options.post_action === 'delete') {
        var dir = mergedDirName();
        var ok = true;
        try {
          ok = window.confirm(
            '合并完成后将永久删除参与合并的源视频，删除后无法恢复。\n\n' +
            '· 「直接拼接」中单独成组（只有 1 个文件）的视频不会被删除。\n' +
            '· 建议先确认成品视频正常，再执行删除。\n' +
            '· 想更稳妥的话，可以改用「移动到「' + dir + '」文件夹」。\n\n' +
            '确定要删除吗？');
        } catch (e) { /* 无法弹窗时按用户的选择执行 */ }
        if (!ok) { return; }
      }
    }

    var button = $('btnStart');
    button.disabled = true;
    api('/api/jobs', { method: 'POST', body: { type: kind, files: files, options: options } })
      .then(function () {
        toast('任务已提交，可在「任务」中查看进度');
        state.picked.clear();
        renderTable();
        renderPicked();
        pollJobs(true);
      })
      .catch(function (err) {
        toast('提交失败：' + err.message, true);
      })
      .then(function () {
        button.disabled = false;
      });
  }

  // ---------------------------------------------------------------- 任务列表

  function pollJobs(force) {
    api('/api/jobs?logs=1').then(function (data) {
      renderJobs(data.jobs || [], data.stats || {});
    }).catch(function () { /* 静默 */ });
  }

  function renderJobs(jobs, stats) {
    var active = (stats.running || 0) + (stats.queued || 0);
    $('tabCount').textContent = String(active);
    var badge = $('taskBadge');
    badge.textContent = '任务 ' + active + ' 进行中 / 共 ' + (stats.total || 0);
    badge.className = 'badge' + (stats.failed ? ' warn' : '');

    var wrap = $('jobsWrap');
    if (!jobs.length) {
      wrap.innerHTML = '<div class="empty">暂无任务</div>';
      return;
    }
    wrap.innerHTML = jobs.map(function (job) {
      var labels = { queued: '排队中', running: '执行中', done: '已完成', failed: '失败', canceled: '已取消' };
      var title = job.type === 'trim' ? '删除片头/片尾' : '合并视频';
      var percent = job.status === 'done' ? 100 : (job.progress || 0);
      var items = job.items || [];
      var itemHtml = items.slice(0, 30).map(function (item) {
        var status = item.status === 'failed' ? ' ✕ ' + (item.error || '') : '';
        var source = item.label
          ? ('第 ' + (item.index || '') + ' 组 · ' + (item.files || 0) + ' 个文件 · ' + item.label)
          : baseName(item.src);
        return '<li class="' + (item.status === 'failed' ? 'failed' : '') + '">' +
          esc(source) + (item.dst ? ' → ' + esc(baseName(item.dst)) : '') + esc(status) + '</li>';
      }).join('');
      var groupText = '';
      if (job.result && job.result.mode === 'copy-grouped') {
        groupText = '分组 ' + (job.result.done_count || 0) + '/' + (job.result.group_count || 0);
      }
      var logs = (job.logs || []).join('\n');
      return '<div class="job ' + job.status + '">' +
        '<div class="job-head">' +
        '<span class="job-title">' + title + '</span>' +
        '<span class="status ' + job.status + '">' + (labels[job.status] || job.status) + '</span>' +
        '<span class="job-meta">' + esc(job.step || '') + '</span>' +
        '</div>' +
        '<div class="bar"><i style="width:' + percent.toFixed(1) + '%"></i></div>' +
        '<div class="job-line">' +
        '<span>进度 ' + percent.toFixed(1) + '%</span>' +
        '<span>完成 ' + (job.done || 0) + '/' + (job.total || 0) + '</span>' +
        (job.failed ? '<span style="color:var(--red)">失败 ' + job.failed + '</span>' : '') +
        (job.speed ? '<span>速度 ' + esc(job.speed) + '</span>' : '') +
        (job.result && job.result.mode ? '<span>方式 ' + esc(modeText(job.result.mode)) + '</span>' : '') +
        (groupText ? '<span>' + esc(groupText) + '</span>' : '') +
        (job.error ? '<span style="color:var(--red)">' + esc(job.error) + '</span>' : '') +
        '</div>' +
        (itemHtml ? '<ul class="job-items">' + itemHtml + '</ul>' : '') +
        '<div class="job-actions">' +
        ((job.status === 'running' || job.status === 'queued')
          ? '<button class="btn small" data-cancel="' + job.id + '">取消</button>' : '') +
        '<button class="btn small" data-logs="' + job.id + '">' + (state.logsOpen[job.id] ? '收起日志' : '查看日志') + '</button>' +
        ((job.status === 'done' || job.status === 'failed' || job.status === 'canceled')
          ? '<button class="btn small" data-remove="' + job.id + '">删除记录</button>' : '') +
        '</div>' +
        (state.logsOpen[job.id] ? '<pre class="logs">' + esc(logs) + '</pre>' : '') +
        '</div>';
    }).join('');

    wrap.querySelectorAll('button[data-cancel]').forEach(function (button) {
      button.addEventListener('click', function () {
        api('/api/jobs/cancel', { method: 'POST', body: { id: button.getAttribute('data-cancel') } })
          .then(function () { pollJobs(true); })
          .catch(function (err) { toast(err.message, true); });
      });
    });
    wrap.querySelectorAll('button[data-remove]').forEach(function (button) {
      button.addEventListener('click', function () {
        api('/api/jobs/remove', { method: 'POST', body: { id: button.getAttribute('data-remove') } })
          .then(function () { pollJobs(true); })
          .catch(function (err) { toast(err.message, true); });
      });
    });
    wrap.querySelectorAll('button[data-logs]').forEach(function (button) {
      button.addEventListener('click', function () {
        var id = button.getAttribute('data-logs');
        state.logsOpen[id] = !state.logsOpen[id];
        pollJobs(true);
      });
    });
  }

  function modeText(mode) {
    return {
      'two-stage': '精准切割',
      'three-stage': '精准切割（片头+片尾）',
      'copy': '直接复制/拼接',
      'fast': '快速复制',
      'reencode': '重新编码',
      'copy-grouped': '按参数分组拼接'
    }[mode] || mode;
  }

  // ---------------------------------------------------------------- 事件绑定

  function bind() {
    document.querySelectorAll('.tab').forEach(function (tab) {
      tab.addEventListener('click', function () {
        document.querySelectorAll('.tab').forEach(function (t) { t.classList.remove('active'); });
        document.querySelectorAll('.view').forEach(function (v) { v.classList.remove('active'); });
        tab.classList.add('active');
        var name = tab.getAttribute('data-tab');
        $('view-' + name).classList.add('active');
        if (name === 'tasks') { pollJobs(true); }
      });
    });

    document.querySelectorAll('.mode').forEach(function (button) {
      button.addEventListener('click', function () {
        document.querySelectorAll('.mode').forEach(function (b) { b.classList.remove('active'); });
        button.classList.add('active');
        state.mode = button.getAttribute('data-mode');
        $('modeTrim').classList.toggle('hidden', state.mode !== 'trim');
        $('modeMerge').classList.toggle('hidden', state.mode !== 'merge');
        renderPicked();
      });
    });

    $('crumbs').addEventListener('click', function (event) {
      var target = event.target.closest('button');
      if (!target) { return; }
      if (target.getAttribute('data-action') === 'back') {
        state.listMode = 'browse';
        renderCrumbs();
        renderTable();
        return;
      }
      browse(target.getAttribute('data-path'));
    });

    $('rootsBar').addEventListener('click', function (event) {
      var chip = event.target.closest('button[data-root]');
      if (!chip) { return; }
      browse(chip.getAttribute('data-root'));
    });

    $('btnUp').addEventListener('click', function () {
      if (state.listMode === 'scan') {
        state.listMode = 'browse';
        renderRoots();
        renderCrumbs();
        renderTable();
        updateUpButton();
        return;
      }
      if (state.listMode === 'cover') {
        state.listMode = state.scanned.length ? 'scan' : 'browse';
        renderRoots();
        renderCrumbs();
        renderTable();
        updateUpButton();
        return;
      }
      var root = activeRoot();
      if (!state.cwd || state.cwd === root) {
        toast('已经是授权目录的顶层了');
        return;
      }
      var parts = state.cwd.split('/');
      parts.pop();
      var parent = parts.join('/') || '/';
      if (root && parent.length < root.length) { parent = root; }
      browse(parent);
    });

    $('btnScan').addEventListener('click', scanCurrent);

    $('btnCoverGroup').addEventListener('click', runCoverGroup);

    $('onlyDup').addEventListener('change', function () { renderTable(); });

    $('btnSelectAllVideos').addEventListener('click', function () {
      if (state.listMode === 'cover') {
        var onlyDup = !!$('onlyDup').checked;
        var paths = [];
        (state.coverGroups || []).forEach(function (group) {
          if (onlyDup && group.count < 2) { return; }
          group.items.forEach(function (item) { paths.push(item.path); });
        });
        if (!paths.length) { toast('当前没有可选的视频', true); return; }
        paths.forEach(function (path) {
          if (!state.picked.has(path)) {
            state.picked.set(path, { path: path, name: baseName(path) });
          }
        });
        renderTable();
        renderPicked();
        toast('已选中 ' + paths.length + ' 个文件');
        return;
      }
      var rows = state.listMode === 'scan' ? state.scanned : state.videos;
      if (!rows.length) { toast('当前列表没有视频', true); return; }
      rows.forEach(function (row) { pick(row.path); });
      renderTable();
      toast('已选中 ' + rows.length + ' 个文件');
    });

    $('btnMergeAnalyze').addEventListener('click', analyzeMergeGroups);
    $('btnMergeGroupClear').addEventListener('click', clearMergeGroups);
    $('mergeMode').addEventListener('change', function () {
      if (!state.mergeGroups) { return; }
      renderMergeGroups();
    });
    $('mergeGroup').addEventListener('change', function () {
      renderMergeGroup();
      // 分组方式变了，旧的分组预览已经不对，直接清掉避免误导
      clearMergeGroups();
    });
    $('orientationRatio').addEventListener('input', function () {
      renderMergeGroup();
      clearMergeGroups();
    });
    $('mergePostAction').addEventListener('change', function () {
      renderMergePost();
      // 分组预览里同步标注「单独成组不会被处理」
      if (state.mergeGroups) { renderMergeGroups(); }
    });

    $('btnClearPicked').addEventListener('click', function () {
      state.picked.clear();
      renderTable();
      renderPicked();
    });

    $('btnClearDone').addEventListener('click', function () {
      api('/api/jobs/clear', { method: 'POST', body: {} })
        .then(function () { pollJobs(true); })
        .catch(function (err) { toast(err.message, true); });
    });

    $('btnSaveConfig').addEventListener('click', function () {
      var patch = {
        trim_mode: $('cfgTrimMode').value,
        concurrency: parseInt($('cfgConcurrency').value, 10),
        on_exists: $('cfgOnExists').value,
        crf: parseInt($('cfgCrf').value, 10),
        preset: $('cfgPreset').value,
        hwaccel: $('cfgHwaccel').value,
        output_mode: $('cfgOutputMode') ? $('cfgOutputMode').value : 'default'
      };
      api('/api/config', { method: 'POST', body: patch })
        .then(function (data) {
          state.cfg = data.config || state.cfg;
          applyConfigToForm();
          state.outputTouched = false;
          applyOutputMode();
          $('saveHint').textContent = '已保存 · ' + new Date().toLocaleTimeString();
          toast('设置已保存');
          return loadState();
        })
        .catch(function (err) { toast('保存失败：' + err.message, true); });
    });

    $('filterInput').addEventListener('input', function () {
      state.filter = $('filterInput').value.trim();
      renderTable();
    });

    $('headInput').addEventListener('input', function () {
      var seconds = parseDuration($('headInput').value);
      $('headHint').textContent = seconds > 0 ? ('解析为 ' + seconds.toFixed(2) + ' 秒') : '请输入时长，例如 90 或 00:01:30';
    });

    $('headChips').addEventListener('click', function (event) {
      var button = event.target.closest('button[data-sec]');
      if (!button) { return; }
      $('headInput').value = fmtSeconds(parseInt(button.getAttribute('data-sec'), 10));
      $('headInput').dispatchEvent(new Event('input'));
    });

    $('trimTarget').addEventListener('change', renderTrimTarget);

    $('tailInput').addEventListener('input', function () {
      var seconds = parseDuration($('tailInput').value);
      $('tailHint').textContent = seconds > 0
        ? ('解析为 ' + seconds.toFixed(2) + ' 秒')
        : '请输入时长，例如 30 或 00:00:30';
    });

    $('tailChips').addEventListener('click', function (event) {
      var button = event.target.closest('button[data-sec]');
      if (!button) { return; }
      $('tailInput').value = fmtSeconds(parseInt(button.getAttribute('data-sec'), 10));
      $('tailInput').dispatchEvent(new Event('input'));
    });

    var outModeSelect = $('cfgOutputMode');
    if (outModeSelect) {
      outModeSelect.addEventListener('change', function () {
        state.cfg.output_mode = outModeSelect.value;
        state.outputTouched = false;
        applyOutputMode();
      });
    }

    $('btnUseCurrent').addEventListener('click', function () {
      if (!state.cwd) { toast('请先进入一个目录', true); return; }
      state.outputTouched = true;
      $('outputDir').value = state.cwd;
      toast('输出目录已设为：' + state.cwd);
    });

    $('btnUseDefault').addEventListener('click', function () {
      state.outputTouched = true;
      $('outputDir').value = state.defaultOutput || (state.roots[0] || '');
      toast('已填入默认输出目录');
    });

    $('btnSortName').addEventListener('click', function () {
      var items = [];
      state.picked.forEach(function (item) { items.push(item); });
      items.sort(function (a, b) { return a.name.localeCompare(b.name, 'zh-CN', { numeric: true }); });
      state.picked = new Map(items.map(function (item) { return [item.path, item]; }));
      renderPicked();
      renderTable();
    });

    $('btnReverse').addEventListener('click', function () {
      var items = [];
      state.picked.forEach(function (item) { items.push(item); });
      items.reverse();
      state.picked = new Map(items.map(function (item) { return [item.path, item]; }));
      renderPicked();
      renderTable();
    });

    $('btnStart').addEventListener('click', startJob);
  }

  function startPolling() {
    if (state.polling) { clearInterval(state.polling); }
    state.polling = setInterval(function () { pollJobs(); }, 1500);
  }

  document.addEventListener('DOMContentLoaded', function () {
    bind();
    renderTrimTarget();
    renderMergePost();
    renderMergeGroup();
    $('headInput').dispatchEvent(new Event('input'));
    $('tailInput').dispatchEvent(new Event('input'));
    loadState().then(function () {
      startPolling();
      // 后端返回的文件夹名/比例默认值拿到后再刷新一次提示文案
      renderMergePost();
      renderMergeGroup();
    });
  });
})();
