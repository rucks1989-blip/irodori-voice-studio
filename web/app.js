const $ = id => document.getElementById(id);
const savedTheme=localStorage.getItem("irodori-theme");
const initialTheme=savedTheme||((window.matchMedia&&window.matchMedia("(prefers-color-scheme: light)").matches)?"light":"dark");
document.documentElement.dataset.theme=initialTheme;
function updateThemeButton(){
  const light=document.documentElement.dataset.theme==="light";
  $("themeToggle").textContent=light?"☾":"☀";
  $("themeToggle").title=light?"ダークモードへ切り替える":"ライトモードへ切り替える";
}
document.addEventListener("DOMContentLoaded",updateThemeButton);
const llmSettingsPanel=document.createElement("div");
llmSettingsPanel.className="llm-settings-inline";
llmSettingsPanel.innerHTML=`<h3>LLMをかんたん設定</h3><p class="hint">GGUFを <code>llm</code> フォルダーへ入れるか、既存のOllamaモデルを選ぶだけで使えます。</p><div class="llm-actions"><button id="scanLlm" class="primary" type="button">自動で探す</button><button id="openLlmFolder" class="secondary" type="button">llmフォルダーを開く</button><button id="deepScanLlm" class="secondary" type="button">全ドライブを詳しく探す</button></div><p id="llmSearchMessage" class="message"></p><div id="llmCandidates" class="llm-candidates muted">「自動で探す」を押してください。</div><div class="llm-test"><button id="testLlm" class="secondary" type="button">選択中のLLMで返答テスト</button><p id="llmTestResult" class="message"></p></div><details class="llm-advanced"><summary>詳細な接続設定</summary><label>OpenAI互換Chat API<input id="llm_endpoint"></label><label>ヘルスチェックURL<input id="llm_health_url"></label><label>モデル名<input id="llm_model"></label><div class="form-grid"><label>Temperature<input id="llm_temperature" type="number" min="0" max="2" step="0.05"></label><label>最大トークン<input id="llm_max_tokens" type="number" min="1" max="4096"></label></div><button class="save primary" type="button">詳細設定を保存</button></details>`;
document.querySelector(".llm-example").appendChild(llmSettingsPanel);
const fields = ["audio_cpp_url","audio_cpp_health_url","model","num_steps","seed","target_chars","backtrack_chars","silence_sentence_ms","silence_dialogue_ms","silence_hard_ms","fade_ms","chunk_wav_retention","log_retention_days","max_diagnostic_jobs","default_voice","llm_endpoint","llm_health_url","llm_model","llm_temperature","llm_max_tokens"];
const checks = ["reader_extraction_enabled","speaker_switch_enabled"];
let currentSettings = {};
let speakerAliases = {};
let quickVoice = null;
let quickVoiceRead = Promise.resolve();
let quickVoiceVersion = 0;
let activeLongJobId="";
let longJobTimer=0;

function escapeHtml(value){const node=document.createElement("span");node.textContent=value;return node.innerHTML}
async function api(url, options={}){const response=await fetch(url,{cache:"no-store",...options});const data=await response.json();if(!response.ok)throw new Error(data.error||"処理に失敗しました。");return data}

function formatBytes(value){if(!value)return "不明";const units=["B","KB","MB","GB","TB"];let size=Number(value),i=0;while(size>=1024&&i<units.length-1){size/=1024;i++}return `${size.toFixed(i>1?1:0)} ${units[i]}`}
function llmButton(label,action,data){return `<button type="button" class="secondary" data-llm-action="${action}" ${Object.entries(data).map(([key,value])=>`data-${key}="${encodeURIComponent(value)}"`).join(" ")}>${label}</button>`}
function renderLlmCandidates(data){
  const rows=[];
  const ollama=data.ollama||{};
  const llamaCpp=data.llama_cpp||{};
  const localFolder=(data.llm_folder||"").toLocaleLowerCase();
  (ollama.models||[]).forEach(item=>rows.push(`<article class="llm-candidate"><div><strong>Ollama：${escapeHtml(item.name)}</strong><small>${formatBytes(item.size)}・既存モデル</small></div><div>${llmButton("そのまま使う","select-ollama",{model:item.name})}${llmButton("llmフォルダーへコピーして使う","copy-ollama",{model:item.name})}</div></article>`));
  (data.gguf||[]).forEach(item=>{const isLocal=item.path.toLocaleLowerCase().startsWith(localFolder+"\\");const gpuButton=item.valid&&llamaCpp.available?llmButton("対応llama.cppでGPU使用","select-llama-cpp",{path:item.path}):"";rows.push(`<article class="llm-candidate ${item.valid?"":"invalid"}"><div><strong>${escapeHtml(item.name)}</strong><small>${formatBytes(item.size)}・${escapeHtml(item.reason)}<br>${escapeHtml(item.path)}</small></div><div>${gpuButton}${item.valid?llmButton(isLocal?"Ollamaへ登録":"llmへコピーしてOllamaで使う","select-gguf",{path:item.path}):""}</div></article>`)});
  $("llmCandidates").innerHTML=rows.length?rows.join(""):`<p>利用可能なLLMは見つかりませんでした。<br><code>${escapeHtml(data.llm_folder||"llm")}</code> へGGUFを入れて再検索してください。</p>`;
}
async function discoverLlm(deep=false){
  const button=deep?$("deepScanLlm"):$("scanLlm");button.disabled=true;$("llmSearchMessage").textContent=deep?"全ドライブを検索しています。時間がかかる場合があります…":"LLMを探しています…";
  try{const data=await api(`/api/llm/discover?deep=${deep?1:0}`);renderLlmCandidates(data);$("llmSearchMessage").textContent=`Ollama ${data.ollama?.models?.length||0}件、GGUF ${data.gguf?.length||0}件を確認しました。`}
  catch(error){$("llmSearchMessage").textContent=error.message}finally{button.disabled=false}
}

let llmDiscovered=false;
document.querySelectorAll(".tabs button").forEach(button=>button.addEventListener("click",()=>{
  document.querySelectorAll(".tabs button,.tab").forEach(item=>item.classList.remove("active"));
  button.classList.add("active");$("tab-"+button.dataset.tab).classList.add("active");
  if(button.dataset.tab==="chat"&&!llmDiscovered){llmDiscovered=true;discoverLlm(false)}
}));

async function refreshStatus(){
  try{const state=await api("/api/health");$("statusDot").className=state.backend_ready?"ok":"error";$("statusText").textContent=state.backend_ready?"準備完了":"audio.cpp未接続";$("statusDetail").textContent=state.backend_ready?`${state.model}・ローカル接続済み`:state.backend_detail;$("llmGuide").classList.toggle("ready",state.llm_ready||state.llm_configured);$("llmGuide").querySelector("strong").textContent=state.llm_ready?`LLM起動中：${state.llm_model}`:state.llm_configured?`LLM設定済み：${state.llm_model}（送信時に起動）`:"LLMを追加すると会話ができます"}
  catch(error){$("statusDot").className="error";$("statusText").textContent="UIエラー";$("statusDetail").textContent=error.message}
}

async function loadSettings(){
  const data=await api("/api/settings");currentSettings=data.settings;
  speakerAliases={...(currentSettings.speaker_markers||{})};
  fields.forEach(id=>{if($(id)&&currentSettings[id]!==undefined)$(id).value=currentSettings[id]});
  checks.forEach(id=>{if($(id))$(id).checked=Boolean(currentSettings[id])});
  $("ignored_texts").value=(currentSettings.ignored_texts||[]).join("\n");
  const selected=currentSettings.default_voice||"";$("default_voice").replaceChildren(new Option("参照音声なし",""));
  $("aliasVoice").replaceChildren(new Option("WAVを選択",""));
  $("chatVoice").replaceChildren(new Option("キャラクター設定またはデフォルトWAV",""));
  data.voice_refs.forEach(name=>{$("default_voice").add(new Option(name,`data/voice_refs/${name}`));$("aliasVoice").add(new Option(name,`data/voice_refs/${name}`));$("chatVoice").add(new Option(name,`data/voice_refs/${name}`))});
  $("default_voice").value=selected;
  $("voiceList").innerHTML=data.voice_refs.length?data.voice_refs.map(name=>`<span>${escapeHtml(name)}</span>`).join(""):"登録済みWAVはありません。";
  $("voiceCount").textContent=`${data.voice_refs.length}件`;
  renderAliases();
}

function collectSettings(){
  const value={};fields.forEach(id=>{if(!$(id))return;value[id]=["num_steps","seed","target_chars","backtrack_chars","silence_sentence_ms","silence_dialogue_ms","silence_hard_ms","fade_ms","log_retention_days","max_diagnostic_jobs"].includes(id)?Number($(id).value):$(id).value});
  checks.forEach(id=>value[id]=$(id).checked);value.ignored_texts=$("ignored_texts").value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);value.speaker_markers={...speakerAliases};return value;
}

$("themeToggle").addEventListener("click",()=>{
  const next=document.documentElement.dataset.theme==="light"?"dark":"light";
  document.documentElement.dataset.theme=next;
  localStorage.setItem("irodori-theme",next);
  updateThemeButton();
});

function renderAliases(){
  const entries=Object.entries(speakerAliases);
  $("aliasList").innerHTML=entries.length?entries.map(([name,path])=>`<div class="alias-item"><span><strong>${escapeHtml(name)}</strong> → ${escapeHtml(path.split(/[\\/]/).pop())}</span><button type="button" data-alias="${encodeURIComponent(name)}">削除</button></div>`).join(""):"明示的な対応はありません。";
}

$("addAliasButton").addEventListener("click",()=>{
  const name=$("aliasName").value.trim(),path=$("aliasVoice").value;
  if(!name||!path){alert("検出文字とWAVを指定してください。");return}
  speakerAliases[name]=path;$("aliasName").value="";$("aliasVoice").value="";renderAliases();
});
$("aliasList").addEventListener("click",event=>{const encoded=event.target.dataset.alias;if(encoded!==undefined){delete speakerAliases[decodeURIComponent(encoded)];renderAliases()}});

document.querySelectorAll(".save").forEach(button=>button.addEventListener("click",async()=>{
  const original=button.textContent;button.disabled=true;
  try{await api("/api/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(collectSettings())});button.textContent="保存しました";await loadSettings();await refreshStatus()}
  catch(error){button.textContent=error.message}finally{setTimeout(()=>{button.textContent=original;button.disabled=false},1300)}
}));

$("text").addEventListener("input",()=>$("charCount").textContent=$("text").value.length);
$("quickVoiceFile").addEventListener("change",()=>{
  const file=$("quickVoiceFile").files[0];
  if(!file)return;
  const version=++quickVoiceVersion;
  $("quickVoiceName").textContent=`${file.name}（読込中）`;
  $("clearQuickVoice").disabled=true;
  quickVoiceRead=readDataUrl(file).then(data=>{if(version!==quickVoiceVersion)return;quickVoice={name:file.name,data};$("quickVoiceName").textContent=`${file.name}（選択中）`;$("clearQuickVoice").disabled=false}).catch(error=>{if(version!==quickVoiceVersion)return;quickVoice=null;$("quickVoiceName").textContent="読み込みに失敗しました";$("message").textContent=error.message;$("clearQuickVoice").disabled=false});
});
$("clearQuickVoice").addEventListener("click",()=>{
  quickVoiceVersion++;quickVoice=null;quickVoiceRead=Promise.resolve();$("quickVoiceFile").value="";$("quickVoiceName").textContent="選択されていません";$("quickVoiceRegister").checked=false;$("clearQuickVoice").disabled=true;
});

function renderPlan(chunks){
  $("plan").innerHTML=chunks.length?chunks.map((item,index)=>`<div class="chunk"><small>${index+1}・${item.length}文字・${escapeHtml(item.reason)}${item.speaker?`・${escapeHtml(item.speaker)}`:""}</small>${escapeHtml(item.text)}</div>`).join(""):"読み上げ対象がありません。";
}

function showGeneratedResult(item){
  $("emptyResult").classList.add("hidden");$("result").classList.remove("hidden");
  $("metricTime").textContent=`${item.seconds} 秒`;$("metricChunks").textContent=item.chunks||item.total;
  $("metricVoices").textContent=item.voices?.length?item.voices.join("、"):"Irodori内部音声";
  $("player").src=item.url;$("download").href=item.url;$("download").download=item.filename;
}
function renderLongJob(job){
  if(!job)return;
  activeLongJobId=job.id;$("longJob").classList.remove("hidden");$("emptyResult").classList.add("hidden");
  const labels={queued:"待機中",running:"長文生成中",cancelling:"停止待ち",cancelled:"停止済み",failed:"一時停止",interrupted:"中断",completed:"完了"};
  $("longJobStatus").textContent=labels[job.status]||job.status;$("longJobPercent").textContent=`${job.progress||0}%`;$("longJobProgress").value=job.progress||0;
  $("longJobMessage").textContent=job.error?`${job.message} ${job.error}`:job.message;
  $("longJobCurrent").textContent=job.total?`${job.current}/${job.total} チャンク${job.current_text?`・現在: ${job.current_text}`:""}`:"";
  $("cancelLongJob").classList.toggle("hidden",!job.can_cancel);$("resumeLongJob").classList.toggle("hidden",!job.can_resume);
  $("generateButton").disabled=job.can_cancel;$("generateButton").textContent=job.can_cancel?"長文を生成中…":"音声を生成";
  if(job.status==="completed"){showGeneratedResult(job);refreshHistory()}
}
async function pollLongJob(jobId){
  clearTimeout(longJobTimer);
  try{const data=await api(`/api/jobs/${encodeURIComponent(jobId)}`);renderLongJob(data.job);if(data.job.can_cancel)longJobTimer=setTimeout(()=>pollLongJob(jobId),1000)}
  catch(error){$("longJobMessage").textContent=error.message;longJobTimer=setTimeout(()=>pollLongJob(jobId),3000)}
}
async function refreshActiveLongJob(){try{const data=await api("/api/jobs/active");if(data.job){renderLongJob(data.job);if(data.job.can_cancel)pollLongJob(data.job.id)}}catch(error){console.warn(error)}}

$("planButton").addEventListener("click",async()=>{
  $("message").textContent="";
  try{const data=await api("/api/plan",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({text:$("text").value})});renderPlan(data.chunks)}catch(error){$("message").textContent=error.message}
});

$("generateForm").addEventListener("submit",async event=>{
  event.preventDefault();const button=$("generateButton");let longStarted=false;clearTimeout(longJobTimer);$("longJob").classList.add("hidden");button.disabled=true;button.textContent="生成しています…";$("message").textContent="";
  try{await quickVoiceRead;const selected=quickVoice;const payload={text:$("text").value,register_voice:$("quickVoiceRegister").checked};if(selected){payload.voice_name=selected.name;payload.voice_data=selected.data}const item=await api("/api/generate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});if(item.long_job){longStarted=true;renderLongJob(item.job);pollLongJob(item.job.id)}else{showGeneratedResult(item);await refreshHistory()}if(selected)$("quickVoiceName").textContent=`${selected.name}（選択中）`;if(selected&&$("quickVoiceRegister").checked)await loadSettings()}
  catch(error){$("message").textContent=error.message}finally{if(!longStarted){button.disabled=false;button.textContent="音声を生成"}}
});

$("cancelLongJob").addEventListener("click",async()=>{if(!activeLongJobId)return;try{const data=await api(`/api/jobs/${encodeURIComponent(activeLongJobId)}/cancel`,{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});renderLongJob(data.job);pollLongJob(activeLongJobId)}catch(error){$("longJobMessage").textContent=error.message}});
$("resumeLongJob").addEventListener("click",async()=>{if(!activeLongJobId)return;try{const data=await api(`/api/jobs/${encodeURIComponent(activeLongJobId)}/resume`,{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});renderLongJob(data.job);pollLongJob(activeLongJobId)}catch(error){$("longJobMessage").textContent=error.message}});

async function refreshHistory(){
  const data=await api("/api/history");$("history").innerHTML=data.items.length?data.items.map(item=>`<div class="history-item"><div><strong>${escapeHtml(item.filename)}</strong><p>${escapeHtml(item.text)}</p></div><div><small>${escapeHtml(item.created_at)}・${item.chunks} chunks</small><br><a href="${item.url}" download>WAV</a></div></div>`).join(""):"<p class='muted'>まだ生成履歴はありません。</p>";
}

function readDataUrl(file){return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(new Error("WAVを読み取れません。"));reader.readAsDataURL(file)})}
$("voiceFiles").addEventListener("change",()=>{$("selectedVoiceCount").textContent=`${$("voiceFiles").files.length}ファイル選択中`});
$("uploadButton").addEventListener("click",async()=>{
  const files=[...$("voiceFiles").files];if(!files.length)return;
  const button=$("uploadButton");button.disabled=true;
  try{for(const file of files){await api("/api/upload-voice",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:file.name,data:await readDataUrl(file)})})}await loadSettings();$("voiceFiles").value=""}
  catch(error){alert(error.message)}finally{button.disabled=false;$("selectedVoiceCount").textContent="ファイル未選択"}
});

async function loadCharacters(selected=""){
  const data=await api("/api/characters");
  $("chatCharacter").replaceChildren();
  data.characters.forEach(item=>$("chatCharacter").add(new Option(`${item.name}${item.sample?"（サンプル）":""}`,item.file)));
  if(selected)$("chatCharacter").value=selected;
}

function addChatMessage(role,text,audioUrl=""){
  const empty=$("chatMessages").querySelector(".chat-empty");if(empty)empty.remove();
  const row=document.createElement("article");row.className=`chat-message ${role}`;
  const p=document.createElement("p");p.textContent=text;row.appendChild(p);
  if(audioUrl){const audio=document.createElement("audio");audio.controls=true;audio.src=audioUrl;audio.autoplay=true;row.appendChild(audio)}
  $("chatMessages").appendChild(row);$("chatMessages").scrollTop=$("chatMessages").scrollHeight;
}

$("uploadCharacter").addEventListener("click",async()=>{
  const file=$("characterFile").files[0];if(!file)return;
  const button=$("uploadCharacter");button.disabled=true;
  try{const result=await api("/api/upload-character",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:file.name,text:await file.text()})});await loadCharacters(result.character.file);$("characterFile").value=""}
  catch(error){alert(error.message)}finally{button.disabled=false}
});

$("chatForm").addEventListener("submit",async event=>{
  event.preventDefault();const message=$("chatInput").value.trim();if(!message)return;
  const button=$("chatSend");button.disabled=true;$("chatInput").value="";addChatMessage("user",message);
  try{const result=await api("/api/chat",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({message,character:$("chatCharacter").value,voice:$("chatVoice").value})});addChatMessage("assistant",result.reply,result.url)}
  catch(error){addChatMessage("error",error.message)}finally{button.disabled=false}
});

$("resetChat").addEventListener("click",async()=>{
  await api("/api/chat/reset",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({character:$("chatCharacter").value})});
  $("chatMessages").innerHTML='<div class="chat-empty">会話履歴を消去しました。</div>';
});

$("scanLlm").addEventListener("click",()=>discoverLlm(false));
$("deepScanLlm").addEventListener("click",()=>discoverLlm(true));
$("openLlmFolder").addEventListener("click",async()=>{try{await api("/api/llm/open-folder",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"})}catch(error){$("llmSearchMessage").textContent=error.message}});
$("llmCandidates").addEventListener("click",async event=>{
  const button=event.target.closest("[data-llm-action]");if(!button)return;
  button.disabled=true;const action=button.dataset.llmAction;$("llmSearchMessage").textContent=action==="copy-ollama"?"Ollamaモデルをllmフォルダーへコピーしています…":"LLMを設定しています…";
  try{
    if(action==="select-ollama")await api("/api/llm/select",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({source:"ollama",model:decodeURIComponent(button.dataset.model)})});
    if(action==="copy-ollama"){
      const copied=await api("/api/llm/copy-ollama",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({model:decodeURIComponent(button.dataset.model)})});
      await api("/api/llm/select",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({source:"gguf",path:copied.gguf.path})});
    }
    if(action==="select-gguf")await api("/api/llm/select",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({source:"gguf",path:decodeURIComponent(button.dataset.path)})});
    if(action==="select-llama-cpp")await api("/api/llm/select",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({source:"llama_cpp",path:decodeURIComponent(button.dataset.path)})});
    await loadSettings();await refreshStatus();$("llmSearchMessage").textContent="LLMを設定しました。返答テストを実行できます。";await discoverLlm(false);
  }catch(error){$("llmSearchMessage").textContent=error.message}finally{button.disabled=false}
});
$("testLlm").addEventListener("click",async()=>{const button=$("testLlm");button.disabled=true;$("llmTestResult").textContent="LLMを起動して返答を生成しています…";try{const result=await api("/api/llm/test",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({message:"こんにちは。リリーとして短く自己紹介して。"})});$("llmTestResult").textContent=`成功：${result.reply}`}catch(error){$("llmTestResult").textContent=error.message}finally{button.disabled=false;await refreshStatus()}});

Promise.all([loadSettings(),loadCharacters(),refreshStatus(),refreshHistory(),refreshActiveLongJob()]).catch(error=>$("message").textContent=error.message);
setInterval(refreshStatus,10000);
