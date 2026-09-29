const params = new URLSearchParams(location.search);
const accountId = params.get('account');
const autoSave = params.get('mode') === 'login';
const desktop = document.querySelector('#desktop');
const message = document.querySelector('#message');
const placeholder = document.querySelector('#placeholder');
const reopen = document.querySelector('#reopen');
const save = document.querySelector('#save-login');
const close = document.querySelector('#close-browser');
let token, sessionId, timer, running = false, saving = false, lastState, readOnly = false;
async function request(body) {
  const response = await fetch(`/api/accounts/${encodeURIComponent(accountId)}/browser`, {
    method:'POST', headers:{'Content-Type':'application/json','X-Console-Token':token},
    body:JSON.stringify({session_id:sessionId,...body})
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '浏览器连接失败');
  return result;
}
function notifySaved() {
  window.opener?.postMessage({type:'account-login-saved',accountId},location.origin);
  const channel = new BroadcastChannel('account-browser');
  channel.postMessage({type:'account-login-saved',accountId});
  channel.close();
}
function render(result) {
  readOnly = result.read_only === true;
  const profileAuth = result.profile_auth === true;
  document.querySelector('#title').textContent = `${result.name} · ${readOnly ? '采集画面' : '浏览器'}`;
  document.title = `${result.name} · ${readOnly ? '采集画面' : '账号浏览器'}`;
  const finished = ['closed','failed'].includes(result.state);
  if (result.viewer_url && !desktop.getAttribute('src')) {
    desktop.src = result.viewer_url;
    desktop.hidden = false;
    placeholder.hidden = true;
    document.querySelector('#engine').textContent = 'KasmVNC';
  }
  save.hidden = close.hidden = readOnly;
  save.textContent = profileAuth ? '确认登录并关闭' : '保存登录并关闭';
  if (!readOnly && profileAuth) document.querySelector('#hint').textContent = '登录状态保存在浏览器环境中；完成登录后点击“确认登录并关闭”即可用于采集。';
  save.disabled = readOnly || result.state !== 'open' || saving;
  close.disabled = readOnly || finished || saving;
  if (readOnly) document.querySelector('#hint').textContent = '只读实时画面；关闭此页面不会停止采集任务。';
  if (result.state !== lastState) {
    message.textContent = result.error || ({opening:readOnly ? '正在连接采集画面…' : '正在打开该账号的浏览器…',open:readOnly ? (result.transport === 'native' ? '采集正在本机浏览器窗口中运行。' : '正在实时观看采集浏览器；画面为只读。') : result.transport === 'native' ? '已打开本机浏览器，请在浏览器窗口操作。' : '已连接该账号的浏览器，可直接点击、滚动和输入。',saving:'正在保存登录，请稍候…',closing:readOnly ? '采集浏览器正在关闭…' : '正在关闭浏览器并释放账号…',closed:readOnly ? '采集画面已结束。' : result.saved ? '登录已保存，可以返回账号列表开始采集。' : '浏览器已关闭，原有登录资料已保留。'}[result.state] || '');
    lastState = result.state;
  }
  if (result.transport === 'native' && result.state === 'open') placeholder.textContent = '请在已打开的本机浏览器窗口中操作';
  if (finished) {
    running = false;
    desktop.removeAttribute('src');
    desktop.hidden = true;
    placeholder.hidden = false;
    placeholder.textContent = message.textContent;
    reopen.hidden = readOnly;
    if (result.saved) notifySaved();
  }
}
async function poll() {
  if (!running) return;
  try { render(await request({kind:'status'})); }
  catch(error) { message.textContent = error.message; }
  if (running) timer = setTimeout(poll,lastState === 'opening' || lastState === 'closing' ? 700 : readOnly ? 2000 : autoSave ? 1000 : 10000);
}
async function openBrowser() {
  clearTimeout(timer);
  reopen.hidden = true;
  lastState = undefined;
  try {
    const response = await fetch('/api/bootstrap');
    if (!response.ok) throw new Error('无法连接平台，请刷新重试。');
    token = (await response.json()).token;
    const result = await request({kind:'open',auto_save:autoSave});
    sessionId = result.id;
    running = true;
    render(result);
    poll();
  } catch(error) {
    running = false;
    message.textContent = error.message;
    placeholder.textContent = error.message;
    reopen.hidden = false;
  }
}
save.onclick = async () => {
  saving = true;
  save.disabled = close.disabled = true;
  message.textContent = '正在保存登录，请稍候…';
  try {
    const result = await request({kind:'save'});
    if (result.error) throw new Error(result.error);
    render(result);
    clearTimeout(timer);
    timer = setTimeout(poll,700);
  } catch(error) { message.textContent = error.message; save.disabled = close.disabled = false; }
  finally { saving = false; }
};
close.onclick = async () => {
  close.disabled = true;
  try { render(await request({kind:'close'})); }
  catch(error) { message.textContent = error.message; close.disabled = false; }
};
reopen.onclick = openBrowser;
// The lease expires after this page is closed; no screenshot or input polling.
if (autoSave) document.querySelector('#hint').textContent = '完成登录后会自动检测并保存；也可手动保存。';
if (accountId) openBrowser(); else { message.textContent = placeholder.textContent = '请从账号列表打开浏览器。'; }
