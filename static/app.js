const $ = (id) => document.getElementById(id);
const localToken = document.querySelector('meta[name="v2t-token"]').content;
let sourceType = 'url';
let selectedFile = null;
let resultTitle = '文字稿';
let busy = false;
let initialized = false;
let assessmentVersion = 0;
let assessmentTimer = null;
let activeTask = null;
let taskVersion = 0;
let taskTimer = null;
let cancelInFlight = false;
let stopAwaitingConfirmation = false;
let taskSubmissionPending = false;
let queueStartPending = false;
let cancelAfterSubmission = false;
let actionPending = false;
let queueLoaded = false;
let connectionUnknown = false;
let queueState = { running: false, active: null, pending: [], recent: [] };
let displayedTaskId = null;
let transcriptDirty = false;
let followLatestResults = true;
const seenFinishedTasks = new Set();
const terminalTaskStates = new Set(['completed', 'failed', 'cancelled']);

function messageAt(id, message) {
  $(id).textContent = message;
  $(id).hidden = !message;
}

function showError(message) { messageAt('error-message', message); }

function readableError(error) {
  return error instanceof TypeError
    ? '连接中断，请确认本地程序仍在运行；服务重启后请刷新页面。'
    : error.message;
}

function normalizeVideoUrl(value) {
  let address = value.trim();
  if (!address) throw new Error('请输入视频链接。');
  if (address.startsWith('//')) address = 'https:' + address;
  else if (/^[a-z][a-z\d+.-]*:/i.test(address) && !/^[^/:]+:\d+(?:[/#?]|$)/.test(address)) {
    if (!/^https?:\/\//i.test(address)) throw new Error('视频链接只支持 http:// 或 https://。');
  } else address = 'https://' + address;
  const url = new URL(address);
  if (!['http:', 'https:'].includes(url.protocol) || !url.hostname) throw new Error('视频链接无效。');
  if (['bilibili.com', 'youtube.com'].includes(url.hostname)) url.hostname = 'www.' + url.hostname;
  return url.href;
}

async function api(path, body, acceptReport = false) {
  const options = body === undefined ? {} : {
    method: 'POST',
    headers: { 'X-V2T-Token': localToken },
    body: body instanceof FormData ? body : JSON.stringify(body),
  };
  if (body !== undefined && !(body instanceof FormData)) options.headers['Content-Type'] = 'application/json';
  const response = await fetch(path, options);
  let result;
  try { result = await response.json(); }
  catch { throw new Error('服务没有返回有效结果，请查看本地程序窗口后重试。'); }
  if (!response.ok && !(acceptReport && (result.messages || result.save))) {
    const error = new Error(result.error || '操作失败（' + response.status + '），请重试。');
    error.status = response.status;
    throw error;
  }
  return result;
}

function currentSettings() {
  return {
    backend: $('backend').value,
    model: $('model').value,
    device: $('device').value,
    model_root: $('model-root').value.trim(),
    output_dir: $('output-dir').value.trim(),
    language: $('language').value,
    api_base_url: $('api-base-url').value.trim(),
    api_model: $('api-model').value.trim(),
    api_key: $('api-key').value,
    clear_api_key: $('clear-api-key').checked,
    api_chunk_seconds: Number($('api-chunk-seconds').value),
  };
}

function updateMode() {
  const local = $('backend').value === 'local';
  $('model-field').hidden = !local;
  $('local-settings').hidden = !local;
  $('api-settings').hidden = local;
  $('prepare-button').hidden = !local;
  $('runtime-install-button').hidden = !local;
  $('runtime-install-note').hidden = !local;
  $('local-assessment-note').hidden = !local;
  $('backend-note').textContent = local
    ? '语音识别在本机完成。视频链接和首次下载模型需要联网。'
    : '识别时，音频会发送到你配置的 API 服务，并可能产生服务商费用。';
  const name = local ? 'Whisper ' + $('model').value : 'API · ' + ($('api-model').value.trim() || '待配置');
  $('config-summary').textContent = name;
  $('active-model').textContent = name;
  const deviceLabel = { auto: '自动选择设备', cuda: '指定 NVIDIA GPU', cpu: '指定 CPU' }[$('device').value];
  $('active-model-note').textContent = local ? deviceLabel + ' · 仅下载所选档位' : '音频将发送至配置的 API 服务';
  $('footer-mode').textContent = local ? '本地模型 · 无需 API Key' : 'API 模式 · 使用自己的服务与密钥';
  $('api-key').disabled = $('clear-api-key').checked;
}

function applySettings(settings) {
  const fields = {
    backend: 'backend', model: 'model', device: 'device', model_root: 'model-root', output_dir: 'output-dir',
    language: 'language', api_base_url: 'api-base-url', api_model: 'api-model', api_chunk_seconds: 'api-chunk-seconds',
  };
  Object.entries(fields).forEach(([key, id]) => { $(id).value = settings[key] ?? (key === 'device' ? 'auto' : ''); });
  $('api-key').value = '';
  $('api-key').placeholder = settings.api_key_set ? '已配置 · 留空保留，输入可替换' : '输入你的密钥';
  $('key-state').textContent = settings.api_key_set ? '已配置（可能来自环境变量）' : '未配置';
  $('clear-api-key').checked = false;
  $('first-run-note').hidden = settings.configured;
  updateMode();
}

async function saveSettings() {
  const result = await api('/api/settings', currentSettings());
  applySettings(result.settings);
  $('settings-status').textContent = '设置已保存';
  messageAt('settings-error', '');
}

function renderAssessment(report) {
  const local = $('backend').value === 'local';
  $('assessment').dataset.status = report.status || (report.ok ? 'ready' : 'blocked');
  $('assessment-title').textContent = report.verified
    ? '本地实测通过'
    : local ? (report.ok ? '本机资源预估' : '本机资源检查未通过')
      : (report.ok ? 'API 配置检查通过' : 'API 配置待完善');
  $('assessment-summary').textContent = report.verified
    ? '加载 ' + report.load_seconds + ' 秒 · 短音频推理 ' + report.inference_seconds + ' 秒'
    : local ? (report.ok ? '可继续使用；运行能力可通过下方自检确认。' : '请按提示调整后再运行。')
      : '这里只检查配置，不会调用远程服务。';
  $('assessment-messages').replaceChildren();
  for (const message of report.messages || []) {
    const item = document.createElement('li');
    item.textContent = message;
    $('assessment-messages').appendChild(item);
  }
  const metrics = [];
  const device = report.resolved_device === 'cuda' ? 'NVIDIA GPU' : report.resolved_device === 'cpu' ? 'CPU' : null;
  const deviceDetails = [];
  if (local && device) deviceDetails.push('实际设备：' + device + (report.compute_type ? ' · ' + report.compute_type.toUpperCase() : ''));
  if (local && report.gpu_name) deviceDetails.push('显卡：' + report.gpu_name);
  if (local && report.free_vram_gb != null) deviceDetails.push('可用显存：' + report.free_vram_gb + ' GiB');
  if (local && $('device').value === 'auto' && report.resolved_device === 'cpu') {
    const reason = report.fallback_reason || (report.messages || []).filter((text) => /GPU|CUDA|显卡|显存|回退|驱动|运行库/i.test(text)).join('；');
    deviceDetails.push('自动模式已选择 CPU' + (reason ? '：' + reason : '；设备不可用原因见下方检查提示。'));
  }
  messageAt('assessment-device', deviceDetails.join(' · '));
  if (local && device) $('active-model-note').textContent = device + (report.compute_type ? ' · ' + report.compute_type.toUpperCase() : '') + (report.gpu_name && report.resolved_device === 'cuda' ? ' · ' + report.gpu_name : '');
  if (report.available_ram_gb != null) metrics.push('可用内存 ' + report.available_ram_gb + ' GiB');
  if (report.free_disk_gb != null) metrics.push('磁盘剩余 ' + report.free_disk_gb + ' GiB');
  if (report.downloaded !== undefined) metrics.push(report.downloaded ? '模型文件已存在' : '预计下载约 ' + report.download_gb + ' GB');
  messageAt('assessment-metrics', metrics.join(' · '));
}

function invalidateAssessment() {
  assessmentVersion += 1;
  clearTimeout(assessmentTimer);
}

function scheduleAssessment(delay = 350) {
  if (!initialized || busy || actionPending) return;
  invalidateAssessment();
  const version = assessmentVersion;
  $('assessment').dataset.status = 'pending';
  $('assessment-title').textContent = '正在检查当前选择…';
  $('assessment-summary').textContent = '更换模型、设备或路径后，检查结果会自动更新。';
  $('assessment-messages').replaceChildren();
  $('assessment-device').hidden = true;
  $('assessment-metrics').hidden = true;
  assessmentTimer = setTimeout(async () => {
    try {
      const report = await api('/api/assess', currentSettings());
      if (version === assessmentVersion && !busy && !actionPending) renderAssessment(report);
    } catch (error) {
      if (version === assessmentVersion && !busy && !actionPending) {
        renderAssessment({ ok: false, status: 'blocked', messages: [readableError(error)] });
      }
    }
  }, delay);
}

function updateCount() {
  const text = $('transcript').value;
  if (text) $('result-count').textContent = Array.from(text.replace(/\s/g, '')).length.toLocaleString() + ' 字符';
  $('copy-button').disabled = !text.trim();
  $('download-button').disabled = !text.trim();
  $('save-edited-button').disabled = busy || actionPending || !text.trim();
}

function inputLocked() {
  return !initialized || !queueLoaded || actionPending;
}

function updateControls() {
  $('settings-fields').disabled = busy || actionPending || !initialized;
  $('form-fields').disabled = inputLocked();
  $('submit-button').disabled = busy || actionPending || !queueLoaded || queueState.pending.length > 0;
  $('submit-button').title = queueState.pending.length ? '已有待执行视频，请使用下方的“开始 / 继续队列”。' : '';
  $('enqueue-button').disabled = inputLocked() || connectionUnknown || queueState.pending.length >= 50;
  $('queue-start-button').disabled = !queueLoaded || connectionUnknown || actionPending || cancelInFlight || queueState.running || !queueState.pending.length;
  $('queue-start-button').textContent = queueState.running ? '队列正在运行' : '开始 / 继续队列';
  $('transcript').readOnly = actionPending;
  document.querySelectorAll('[data-queue-action]').forEach((button) => {
    button.disabled = actionPending || connectionUnknown || button.dataset.boundary === 'true';
  });
  document.querySelectorAll('[data-result-action]').forEach((button) => { button.disabled = actionPending; });
  updateCount();
}

function setBusy(value) {
  const changed = busy !== value;
  busy = value;
  if (value) invalidateAssessment();
  updateControls();
  if (changed && !value && initialized && $('assessment').dataset.status === 'pending') scheduleAssessment(0);
}

function taskName(kind) {
  return { transcribe: '转写任务', prepare: '模型准备与自检', model_prepare: '模型准备与自检', runtime_install: 'GPU 运行库安装' }[kind] || '后台任务';
}

function resetTaskControls() {
  $('submit-label').textContent = '开始转写';
  $('prepare-button').textContent = '下载所选模型并自检';
  $('runtime-install-button').textContent = '按需安装 GPU 运行库（较大下载）';
  $('working-state').hidden = true;
  const hasResult = Boolean($('transcript').value);
  $('result-content').hidden = !hasResult;
  $('empty-state').hidden = hasResult;
  if (!hasResult) $('result-count').textContent = '等待内容';
}

function renderTask(task) {
  const terminal = terminalTaskStates.has(task.status);
  const cancelling = task.status === 'cancelling';
  const labels = { queued: '等待执行', starting: '正在启动', running: '正在运行', cancelling: '正在停止', completed: '已完成', failed: '失败', cancelled: '已停止' };
  $('task-panel').hidden = false;
  $('task-panel').dataset.status = task.status;
  $('task-title').textContent = taskName(task.kind) + ' · ' + (labels[task.status] || '状态待确认');
  const seconds = Math.max(0, Math.floor(Number(task.seconds) || 0));
  const description = cancelling ? '正在终止后台进程，队列已暂停。' : task.stage || (terminal ? '任务已结束。' : '可以继续添加视频或停止当前任务。');
  $('task-description').textContent = (task.label ? task.label + ' · ' : '') + description + ' · ' + seconds + ' 秒';
  $('cancel-task-button').hidden = terminal && !queueState.running;
  $('cancel-task-button').disabled = cancelling || cancelInFlight;
  $('cancel-task-button').textContent = cancelling ? '正在停止…' : '停止任务 / 暂停队列';
  $('task-retry-button').hidden = true;
  messageAt('task-error', task.status === 'failed' ? task.error || '任务失败。' : '');
  if (task.kind === 'transcribe' && !terminal) {
    $('submit-label').textContent = cancelling ? '正在停止…' : '正在处理…';
    const hasResult = Boolean($('transcript').value);
    $('result-content').hidden = !hasResult;
    $('empty-state').hidden = true;
    $('working-state').hidden = hasResult;
    if (!hasResult) $('result-count').textContent = cancelling ? '正在停止' : '处理中';
    $('elapsed-time').textContent = '已运行 ' + seconds + ' 秒';
  } else if (!terminal) {
    messageAt('prepare-progress', taskName(task.kind) + ' · ' + (cancelling ? '正在停止' : task.stage || '正在处理') + ' · ' + seconds + ' 秒');
    $(task.kind === 'runtime_install' ? 'runtime-install-button' : 'prepare-button').textContent = cancelling ? '正在停止…' : '正在处理…';
  }
}

function showTaskResult(task, explicit = false) {
  if (task.kind !== 'transcribe' || task.status !== 'completed') return;
  if (explicit && actionPending) return;
  if (transcriptDirty) {
    if (explicit) showError('当前文字有未保存的编辑，请先保存编辑版，再查看其他结果。');
    return;
  }
  const result = task.result || {};
  displayedTaskId = task.task_id;
  if (explicit) followLatestResults = false;
  resultTitle = result.title || task.label || '文字稿';
  $('transcript').value = result.text || '';
  $('result-title').textContent = resultTitle;
  $('result-method').textContent = (result.method || '') + ' · ' + (result.seconds ?? task.seconds ?? 0) + ' 秒';
  $('copy-button').textContent = '复制文字';
  renderSave(result.save);
  $('result-content').hidden = false;
  $('empty-state').hidden = true;
  $('working-state').hidden = true;
  updateCount();
}

function finishTask(task) {
  if (seenFinishedTasks.has(task.task_id)) return;
  seenFinishedTasks.add(task.task_id);
  if (task.status === 'completed') {
    const result = task.result || {};
    if (task.kind === 'transcribe') {
      if (!displayedTaskId || followLatestResults) showTaskResult(task);
      if (transcriptDirty || !followLatestResults) messageAt('queue-notice', '有新的文字稿已生成，可在“最近任务”中查看。当前显示的文字保持不变。');
    } else if (task.kind === 'runtime_install') {
      messageAt('prepare-progress', (result.messages || ['GPU 运行库安装完成。']).join('；'));
      scheduleAssessment(0);
    } else {
      renderAssessment(result);
      messageAt('prepare-progress', result.ok ? '自检完成 · 共 ' + (result.seconds ?? task.seconds ?? 0) + ' 秒' : '自检未通过，请查看上方提示。');
    }
  } else if (task.status === 'failed') {
    const error = typeof task.error === 'string' ? task.error : '后台任务失败，请查看本地程序提示。';
    messageAt(task.kind === 'transcribe' ? 'queue-notice' : 'settings-error', (task.label || taskName(task.kind)) + '：' + error);
  } else if (task.kind !== 'transcribe') {
    messageAt('prepare-progress', '任务已停止。已有完整模型文件会保留。');
  }
}

function queueButton(text, action, handler, boundary = false) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'secondary-button' + (action === 'remove' ? ' queue-remove' : '');
  button.textContent = text;
  button.dataset.queueAction = action;
  button.dataset.boundary = String(boundary);
  button.disabled = boundary || actionPending || connectionUnknown;
  button.addEventListener('click', handler);
  return button;
}

function renderQueue() {
  const pending = queueState.pending;
  $('queue-count').textContent = pending.length + ' 个等待';
  $('queue-empty').hidden = pending.length > 0;
  $('queue-status').textContent = queueState.running
    ? '按顺序自动处理；运行中可以继续添加视频，调整尚未开始的项目。'
    : activeTask ? '当前任务正在运行；待执行视频可继续添加和排序。'
      : pending.length ? '队列已就绪，点击“开始 / 继续队列”依次转写。' : '队列空闲。';
  $('pending-list').replaceChildren();
  pending.forEach((task, index) => {
    const item = document.createElement('li');
    item.className = 'queue-item';
    const title = document.createElement('strong');
    title.className = 'queue-item-title';
    title.textContent = (index + 1) + '. ' + (task.label || '视频');
    const meta = document.createElement('p');
    meta.className = 'queue-item-meta';
    meta.textContent = task.stage || '等待执行 · 使用加入时的设置';
    const actions = document.createElement('div');
    actions.className = 'queue-item-actions';
    const path = '/api/queue/' + encodeURIComponent(task.task_id);
    actions.append(
      queueButton('↑ 上移', 'up', () => runQueueAction(path + '/move', { direction: 'up' }), index === 0),
      queueButton('↓ 下移', 'down', () => runQueueAction(path + '/move', { direction: 'down' }), index === pending.length - 1),
      queueButton('移除', 'remove', () => runQueueAction(path + '/remove', {}, '已移除待执行视频。')),
    );
    item.append(title, meta, actions);
    $('pending-list').appendChild(item);
  });
  $('recent-tasks').hidden = !queueState.recent.length;
  $('recent-list').replaceChildren();
  for (const task of queueState.recent) {
    const item = document.createElement('li');
    item.className = 'queue-item';
    const title = document.createElement('strong');
    title.className = 'queue-item-title';
    title.textContent = task.label || task.result?.title || taskName(task.kind);
    const meta = document.createElement('p');
    meta.className = 'queue-item-meta';
    const state = { completed: '已完成', failed: '失败', cancelled: '已停止' }[task.status] || task.status;
    const save = task.result?.save;
    meta.textContent = state + (task.error ? ' · ' + task.error : save?.saved ? ' · ' + save.path : '');
    item.append(title, meta);
    if (task.kind === 'transcribe' && task.status === 'completed') {
      const actions = document.createElement('div');
      actions.className = 'queue-item-actions';
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'secondary-button';
      button.dataset.resultAction = 'view';
      button.disabled = actionPending;
      button.textContent = displayedTaskId === task.task_id ? '正在查看' : '查看文字稿';
      button.addEventListener('click', () => { showTaskResult(task, true); renderQueue(); });
      actions.appendChild(button);
      item.appendChild(actions);
    }
    $('recent-list').appendChild(item);
  }
  updateControls();
}

function renderStopNotice(state) {
  stopAwaitingConfirmation = state.active?.status === 'cancelling';
  const message = !state.active && !state.running
    ? '后端已确认任务停止；队列已暂停，待执行项和已保存文件均保留。'
    : stopAwaitingConfirmation
      ? '队列已暂停，正在终止当前任务；等待后端确认。'
      : '停止请求已返回，请查看上方的当前任务状态。';
  messageAt('queue-notice', message);
}

function observeQueue(state, version) {
  if (version !== taskVersion) return;
  if (!state || typeof state.running !== 'boolean' || !Array.isArray(state.pending) || !Array.isArray(state.recent)) {
    throw new Error('队列状态格式不正确，请重新获取状态。');
  }
  queueState = state;
  queueLoaded = true;
  connectionUnknown = false;
  activeTask = state.active;
  resetTaskControls();
  setBusy(Boolean(state.running || activeTask || taskSubmissionPending));
  for (const task of [...state.recent].reverse()) finishTask(task);
  if (activeTask) renderTask(activeTask);
  else if (state.running) {
    $('task-panel').hidden = false;
    $('task-panel').dataset.status = 'running';
    $('task-title').textContent = '正在接续队列';
    $('task-description').textContent = '即将按当前顺序处理下一个视频。';
    $('cancel-task-button').hidden = false;
    $('cancel-task-button').disabled = cancelInFlight;
    $('cancel-task-button').textContent = '停止任务 / 暂停队列';
    $('task-retry-button').hidden = true;
    messageAt('task-error', '');
  } else if (state.recent.length) {
    renderTask(state.recent[0]);
    if (state.recent[0].status === 'cancelled') $('task-description').textContent = '后端已确认任务停止；队列已暂停，待执行项和已保存文件均保留。';
  } else {
    $('task-panel').hidden = true;
    messageAt('task-error', '');
  }
  renderQueue();
  if (stopAwaitingConfirmation) renderStopNotice(state);
}

function scheduleQueuePoll(delay = 1000) {
  clearTimeout(taskTimer);
  if (initialized) taskTimer = setTimeout(refreshQueue, delay);
}

function taskConnectionError(error, version) {
  if (version !== taskVersion) return;
  connectionUnknown = true;
  setBusy(true);
  $('task-panel').hidden = false;
  $('task-title').textContent = '任务与队列状态尚未确认';
  $('task-description').textContent = '后台任务可能仍在运行。正在重新读取状态，也可发送停止请求；请勿重复提交同一视频。';
  messageAt('task-error', readableError(error));
  $('task-retry-button').hidden = false;
  $('cancel-task-button').hidden = false;
  $('cancel-task-button').disabled = cancelInFlight;
  $('cancel-task-button').textContent = '停止任务 / 暂停队列';
  scheduleQueuePoll(2000);
}

async function refreshQueue() {
  const version = ++taskVersion;
  clearTimeout(taskTimer);
  try {
    observeQueue(await api('/api/queue'), version);
    if (version === taskVersion) scheduleQueuePoll(busy || queueState.pending.length ? 1000 : 2500);
  } catch (error) { taskConnectionError(error, version); }
}

async function beginTask(kind, path, body) {
  if (busy || actionPending || !initialized || (kind === 'transcribe' && queueState.pending.length)) return;
  ++taskVersion;
  clearTimeout(taskTimer);
  taskSubmissionPending = true;
  actionPending = true;
  cancelAfterSubmission = false;
  setBusy(true);
  showError('');
  messageAt('settings-error', '');
  activeTask = { kind, status: 'starting', seconds: 0 };
  renderTask(activeTask);
  try {
    await saveSettings();
    if (!cancelAfterSubmission) {
      const task = await api(path, body);
      activeTask = task;
      if (kind === 'transcribe') followLatestResults = true;
      if (cancelAfterSubmission) renderStopNotice(await api('/api/queue/stop', {}));
    } else messageAt('queue-notice', '已取消提交，后台任务尚未启动。');
  } catch (error) {
    messageAt(kind === 'transcribe' ? 'error-message' : 'settings-error', readableError(error));
    if (cancelAfterSubmission) {
      try { renderStopNotice(await api('/api/queue/stop', {})); }
      catch (stopError) { messageAt('task-error', readableError(stopError)); }
    }
  } finally {
    taskSubmissionPending = false;
    actionPending = false;
    cancelAfterSubmission = false;
    await refreshQueue();
  }
}

async function runQueueAction(path, body, notice = '') {
  if (actionPending || !initialized || connectionUnknown) return;
  actionPending = true;
  queueStartPending = path === '/api/queue/start';
  if (queueStartPending) cancelAfterSubmission = false;
  ++taskVersion;
  clearTimeout(taskTimer);
  updateControls();
  showError('');
  if (queueStartPending) {
    $('task-panel').hidden = false;
    $('task-panel').dataset.status = 'starting';
    $('task-title').textContent = '正在启动队列';
    $('task-description').textContent = '正在等待后端确认；需要中止时可以点击停止。';
    $('cancel-task-button').hidden = false;
    $('cancel-task-button').disabled = false;
    $('cancel-task-button').textContent = '停止任务 / 暂停队列';
    $('task-retry-button').hidden = true;
    messageAt('task-error', '');
  }
  try {
    await api(path, body);
    if (queueStartPending) {
      if (cancelAfterSubmission) renderStopNotice(await api('/api/queue/stop', {}));
      else {
        stopAwaitingConfirmation = false;
        messageAt('queue-notice', '');
      }
    }
    if (notice) messageAt('queue-notice', notice);
  } catch (error) {
    showError(readableError(error));
    if (queueStartPending && cancelAfterSubmission) {
      try { renderStopNotice(await api('/api/queue/stop', {})); }
      catch (stopError) { showError('停止请求未完成：' + readableError(stopError)); }
    }
  } finally {
    if (queueStartPending) cancelAfterSubmission = false;
    queueStartPending = false;
    actionPending = false;
    await refreshQueue();
  }
}

$('task-retry-button').addEventListener('click', () => initialized ? refreshQueue() : initialize());
$('queue-start-button').addEventListener('click', () => runQueueAction('/api/queue/start', {}));

async function cancelCurrentTask() {
  if (queueStartPending || (taskSubmissionPending && !activeTask?.task_id)) {
    cancelAfterSubmission = true;
    $('task-title').textContent = '正在等待任务登记后停止';
    $('task-description').textContent = '提交仍在处理中；一旦取得任务编号，会立即停止任务并暂停队列。';
    $('cancel-task-button').disabled = true;
    return;
  }
  if (cancelInFlight) return;
  cancelInFlight = true;
  updateControls();
  ++taskVersion;
  clearTimeout(taskTimer);
  $('cancel-task-button').disabled = true;
  try {
    renderStopNotice(await api('/api/queue/stop', {}));
  } catch (error) { showError('停止请求未完成：' + readableError(error)); }
  finally { cancelInFlight = false; updateControls(); await refreshQueue(); }
}
$('cancel-task-button').addEventListener('click', cancelCurrentTask);

$('settings-form').addEventListener('input', () => {
  if (!initialized || busy || actionPending) return;
  $('settings-status').textContent = '有未保存的设置；开始转写时会自动保存';
  updateMode();
  scheduleAssessment();
});
$('settings-form').addEventListener('change', () => {
  updateMode();
  scheduleAssessment();
});
$('language').addEventListener('change', () => { $('settings-status').textContent = '开始转写时会保存当前语言设置'; });
$('settings-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy || actionPending || !initialized) return;
  actionPending = true;
  updateControls();
  $('settings-status').textContent = '正在保存…';
  try { await saveSettings(); }
  catch (error) { $('settings-status').textContent = '设置未保存'; messageAt('settings-error', readableError(error)); }
  finally { actionPending = false; updateControls(); scheduleAssessment(0); }
});

$('prepare-button').addEventListener('click', () => beginTask('prepare', '/api/models/prepare', currentSettings()));
$('runtime-install-button').addEventListener('click', () => beginTask('runtime_install', '/api/runtime/install', currentSettings()));

function setSource(source) {
  if (inputLocked()) return;
  sourceType = source;
  document.querySelectorAll('.source-tab').forEach((tab) => {
    const active = tab.dataset.source === source;
    tab.classList.toggle('active', active);
    tab.setAttribute('aria-selected', String(active));
  });
  $('url-panel').hidden = source !== 'url';
  $('file-panel').hidden = source !== 'file';
  $('asr-option').hidden = source !== 'url';
  $('advanced-options').hidden = source !== 'url';
  showError('');
}
document.querySelectorAll('.source-tab').forEach((tab) => {
  tab.addEventListener('click', () => setSource(tab.dataset.source));
  tab.addEventListener('keydown', (event) => {
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault();
      setSource(sourceType === 'url' ? 'file' : 'url');
      $(sourceType + '-tab').focus();
    }
  });
});

function selectFile(file) {
  if (inputLocked() || !file) return;
  if (!file.size || file.size > 1024 ** 3) {
    selectedFile = null;
    $('media-file').value = '';
    $('file-name').textContent = '点击选择，或拖入文件';
    $('file-note').textContent = '音频或视频 · 最大 1 GiB';
    showError('请选择大小在 0 到 1 GiB 之间的音视频文件。');
    return;
  }
  selectedFile = file;
  $('file-name').textContent = file.name;
  $('file-note').textContent = (file.size / 1024 ** 2).toFixed(1) + ' MiB · 点击更换';
  showError('');
}
$('dropzone').addEventListener('click', () => $('media-file').click());
$('media-file').addEventListener('change', (event) => selectFile(event.target.files[0]));
['dragover', 'dragleave', 'drop'].forEach((name) => {
  $('dropzone').addEventListener(name, (event) => {
    event.preventDefault();
    $('dropzone').classList.toggle('dragging', name === 'dragover' && !inputLocked());
    if (name === 'drop' && !inputLocked()) {
      if (event.dataTransfer.files.length !== 1) showError('请一次选择一个文件。');
      else selectFile(event.dataTransfer.files[0]);
    }
  });
});

function renderSave(save, edited = false) {
  const saved = Boolean(save && save.saved);
  $('save-status').dataset.saved = String(saved);
  messageAt('save-status', saved
    ? (edited ? '编辑版已保存' : '已自动保存') + '：' + save.path
    : '文字已生成，但本地保存失败' + (save?.attempts ? '（已尝试 ' + save.attempts + ' 次）' : '') + '：' + (save?.error || '未返回保存结果') + '。可修改目录后保存，或点击“另存 TXT”。');
  $('edit-note').textContent = saved ? (edited ? '当前文字已保存' : '原稿已自动保存') : '文字尚未保存';
}
$('transcript').addEventListener('input', () => {
  transcriptDirty = true;
  updateCount();
  $('edit-note').textContent = '有未保存的编辑';
});

function sourceForm() {
  const form = new FormData();
  form.append('source_type', sourceType);
  form.append('language', $('language').value);
  if (sourceType === 'url') {
    try {
      const url = normalizeVideoUrl($('video-url').value);
      $('video-url').value = url;
      form.append('url', url);
    } catch {
      $('video-url').focus();
      throw new Error('请输入有效的视频链接，例如 bilibili.com/video/…；仅支持 HTTP 或 HTTPS。');
    }
    form.append('force_asr', String($('force-asr').checked));
    const cookies = $('cookies-file').files[0];
    if (cookies) {
      if (cookies.size > 1024 ** 2) throw new Error('Cookie 文件不能超过 1 MiB。');
      form.append('cookies', cookies);
    }
  } else {
    if (!selectedFile) throw new Error('请先选择一个音频或视频文件。');
    form.append('media', selectedFile);
  }

  return form;
}

$('transcribe-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy || inputLocked() || queueState.pending.length) return;
  try { await beginTask('transcribe', '/api/transcribe', sourceForm()); }
  catch (error) { showError(readableError(error)); }
});

$('enqueue-button').addEventListener('click', async () => {
  if (inputLocked() || connectionUnknown || queueState.pending.length >= 50) return;
  let form;
  try { form = sourceForm(); }
  catch (error) { showError(readableError(error)); return; }
  actionPending = true;
  ++taskVersion;
  clearTimeout(taskTimer);
  updateControls();
  $('enqueue-button').textContent = '正在加入…';
  showError('');
  let submitted = false;
  try {
    await saveSettings();
    submitted = true;
    await api('/api/queue', form);
    if (sourceType === 'url') $('video-url').value = '';
    else {
      selectedFile = null;
      $('media-file').value = '';
      $('file-name').textContent = '点击选择，或拖入文件';
      $('file-note').textContent = '音频或视频 · 最大 1 GiB';
    }
    messageAt('queue-notice', '视频已加入队列，可继续添加或调整待执行顺序。');
  } catch (error) {
    showError(readableError(error) + (submitted && !error.status ? ' 请先检查队列中是否已存在该视频，再决定是否重新添加。' : ''));
  } finally {
    actionPending = false;
    $('enqueue-button').textContent = '加入队列 +';
    await refreshQueue();
  }
});

$('save-edited-button').addEventListener('click', async () => {
  if (busy || actionPending || !$('transcript').value.trim()) return;
  actionPending = true;
  updateControls();
  showError('');
  $('save-edited-button').textContent = '保存中…';
  try {
    await saveSettings();
    const result = await api('/api/save', { title: resultTitle, text: $('transcript').value }, true);
    renderSave(result.save, true);
    if (result.save?.saved) transcriptDirty = false;
  } catch (error) { showError(readableError(error)); }
  finally { $('save-edited-button').textContent = '保存编辑版'; actionPending = false; updateControls(); }
});

$('copy-button').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText($('transcript').value);
    $('copy-button').textContent = '已复制';
    setTimeout(() => { $('copy-button').textContent = '复制文字'; }, 1800);
  } catch {
    $('transcript').focus();
    $('transcript').select();
    showError('浏览器未允许复制，已选中文字，可按 Ctrl+C 或 ⌘C 复制。');
  }
});
$('download-button').addEventListener('click', () => {
  const name = resultTitle.replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_').slice(0, 100);
  const url = URL.createObjectURL(new Blob([$('transcript').value + '\n'], { type: 'text/plain;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = 'transcript_' + (name || '文字稿') + '.txt';
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
window.addEventListener('beforeunload', (event) => {
  if (actionPending || transcriptDirty) { event.preventDefault(); event.returnValue = ''; }
});

async function initialize() {
  const version = ++taskVersion;
  clearTimeout(taskTimer);
  setBusy(true);
  try {
    const result = await api('/api/settings');
    if (version !== taskVersion) return;
    $('model').replaceChildren(...result.models.map((model) => new Option(model.label, model.id)));
    applySettings(result.settings);
    $('configuration').open = !result.settings.configured;
    initialized = true;
    await refreshQueue();
    if (!busy) scheduleAssessment(0);
  } catch (error) {
    if (version !== taskVersion) return;
    $('configuration').open = true;
    messageAt('settings-error', readableError(error));
    if (!initialized) $('config-summary').textContent = '配置读取失败';
    taskConnectionError(error, version);
  }
}
initialize();
