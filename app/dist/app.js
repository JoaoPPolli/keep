'use strict';
const $ = id => document.getElementById(id);
const local = ['127.0.0.1', 'localhost'].includes(location.hostname);
let token = '', current = null, jobs = [], session = null, searching = false, toastTimer;
let retrySection = null, startingDownload = false;
const pendingJobActions = new Set();
const jobActionErrors = new Map();
let browserSelectionChanged = false;
let preferences = {paste_and_download:false, default_browser:'none', default_quality:'1080',platform_folders:false,notifications:false};
let inspection = null, currentItems = [], selectedVideos = new Set();
let previewId = '', previewGeneration = 0, previewTimer, previewPlayingSection = false;
let updateStatus = null, updateTimer, refreshTimer, preferencesSaving = false, keepClosed = false;
let batchRunning=false,batchCancel=false,batchSource='',batchEntries=[],batchNextIndex=0,queueBusy=false,queueDragId='',storageCheckedAt=0;
let editSections=[],previewFPS=null,previewDimensions=null,previewFrameTimes=null,previewFrameNote='',cropDrag=null,frameSeekBusy=false;
let retryEditor=null,retryDownloadOptions=null;
let cropFrameCallback=null,cropAnimationFrame=null;
let editorLayout='full',editorRegions={camera:{x:0,y:0,width:.28,height:.28},content:{x:.25,y:0,width:.5,height:1}};
let captionSections=null;
let captionTask='',captionState='idle',captionCues=[],captionBinding='',captionTimer,captionGeneration=0,pendingCaptionCues=null;
let desktopStatus=null,desktopWorking=false,restorePoints=[],restoringPoint=false;
const icons = {YouTube:'yt', X:'x', Twitch:'twitch',Local:'folder'};
const icon = name => `<svg aria-hidden="true"><use href="#${name}"/></svg>`;
const esc = text => String(text ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let defaultQuality = '1080';
try { defaultQuality = localStorage.getItem('keep-quality') || '1080'; } catch {}
if (!['best','2160','1080','720','480'].includes(defaultQuality)) defaultQuality = '1080';
$('default-quality').value = defaultQuality;
function toast(text) { $('toast').textContent=text; $('toast').hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').hidden=true,3500); }
function message(text, error=false) { $('message').textContent=text; $('message').className='message'+(error?' error':''); $('message').hidden=!text; }
function configureQuality(heights = []) {
  for (const option of $('quality').options) {
    option.disabled = option.value !== 'best' && heights.length > 0 && !heights.some(height => height <= Number(option.value));
  }
  $('quality').value = defaultQuality;
  const unavailable = $('quality').selectedOptions[0]?.disabled;
  if (unavailable) $('quality').value = 'best';
  $('quality-note').textContent = unavailable
    ? 'A qualidade padrão não está disponível neste vídeo. Selecionamos Melhor disponível; confira antes de baixar.'
    : 'Qualidade original, até a resolução escolhida. Sem aumentar artificialmente a imagem.';
}
function timeLabel(seconds) {
  const total = Math.round(Number(seconds) * 1000), whole = Math.floor(total / 1000);
  const hours = Math.floor(whole / 3600), minutes = Math.floor(whole / 60) % 60;
  const fraction = total % 1000 ? '.' + String(total % 1000).padStart(3, '0').replace(/0+$/, '') : '';
  return (hours ? hours + ':' + String(minutes).padStart(2, '0') : String(minutes)) + ':' + String(whole % 60).padStart(2, '0') + fraction;
}
function parseTime(text) {
  const value = text.trim().replace(',', '.');
  if (!/^\d+(?::\d{1,2}){0,2}(?:\.\d{1,3})?$/.test(value)) throw new Error('Use minutos:segundos, como 1:30, ou horas:minutos:segundos.');
  const parts = value.split(':').map(Number);
  if (parts.slice(1).some(part => part >= 60)) throw new Error('Os segundos e os minutos depois de uma hora devem ser menores que 60.');
  const seconds = parts.reduce((total, part) => total * 60 + part, 0);
  if (!Number.isFinite(seconds)) throw new Error('Informe um horário válido.');
  return seconds;
}
function selectedSection() {
  if ($('download-scope').value !== 'section') return null;
  const start = parseTime($('section-start').value), end = parseTime($('section-end').value);
  if (end <= start) throw new Error('O fim precisa ser depois do início.');
  if (end - start < 0.0999) throw new Error('Escolha um trecho de pelo menos 0,1 segundo.');
  if (!current || !Number.isFinite(current.duration) || start >= current.duration || end > current.duration + 0.001) throw new Error('Escolha horários dentro da duração do vídeo.');
  return {start, end};
}
function updateSection() {
  const audio=$('format').value==='audio',gif=$('format').value==='gif',count=selectedItems().length;
  if(gif&&count===1&&Number(current?.duration)>0&&session?.ffmpeg)$('download-scope').value='section';
  const partial=$('download-scope').value==='section';

  $('section-fields').hidden = !partial;
  $('cut-preview').hidden = audio||count!==1||!Number(current?.duration);
  $('multi-section-editor').hidden=!partial;
  for(const id of ['mark-start','mark-end','play-section'])$(id).hidden=!partial;
  $('timeline').hidden=!partial;
  $('quality').disabled = audio||gif||!!current?.clip;
  if(current?.clip)$('quality-note').textContent='O download usa a cópia preparada nesta prévia. Clique em Baixar para salvar sua versão.';
  $('scope-full').disabled=gif;
  $('custom-name-hint').textContent='Digite o nome sem extensão. O Keep adiciona '+(gif?'GIF':audio?'MP3':'MP4')+'. Cada download continua em sua própria pasta.';
  $('section-output-note').hidden=!partial||$('section-output').value!=='join'||editSections.length<2;
  const number=partial&&editSections.length>1&&$('section-output').value==='separate'?editSections.length:count;
  $('download-button').innerHTML=icon('download')+(gif?(number>1?`Criar ${number} GIFs`:'Criar GIF'):number>1?`Baixar ${number} ${audio?'áudios':'vídeos'}`:partial?($('section-output').value==='join'&&editSections.length>1?'Baixar montagem':audio?'Baixar áudio do trecho':'Baixar trecho'):audio?'Baixar áudio':'Baixar vídeo');
  $('section-summary').classList.remove('invalid');
  let valid = !current?.clip || !!current.clip.key;
  try {
    if(captionsEnabled()){if(count!==1)throw new Error('Selecione apenas um vídeo para legendar.');captionExportReady();}
    if(verticalEnabled()&&count!==1)throw new Error('Selecione apenas um vídeo para reenquadrar.');
    if(gif&&!partial)throw new Error('Selecione um único vídeo gravado com duração informada para criar um GIF.');
    const sections=plannedSections();
    if(sections){
      const total=sections.reduce((sum,item)=>sum+item.end-item.start,0);
      $('section-summary').textContent=sections.length>1?`${sections.length} trechos · ${timeLabel(total)} no total · ${$('section-output').value==='join'?'um arquivo na ordem da lista':'um arquivo por trecho'}.`:`De ${timeLabel(sections[0].start)} até ${timeLabel(sections[0].end)} · ${timeLabel(total)} de duração.`;
      if(gif)$('section-summary').textContent+=' GIF sem áudio, com até 30 segundos no total.';
    }else $('section-summary').textContent=current&&Number.isFinite(current.duration)&&current.duration>0?`Vídeo inteiro · ${timeLabel(current.duration)} de duração.`:'A plataforma não informou a duração. A seleção de trecho fica indisponível para este vídeo.';
  } catch (error) { valid = false; $('section-summary').textContent = error.message; $('section-summary').classList.add('invalid'); }
  let selectionValid=true;try{selectedSection();}catch{selectionValid=false;}
  $('section-start').setAttribute('aria-invalid',String(!selectionValid));$('section-end').setAttribute('aria-invalid',String(!selectionValid));
  $('add-section').disabled=!partial||!selectionValid||editSections.length>=24||count!==1;
  $('download-button').disabled=!valid||startingDownload||batchRunning||!count||(partial&&count!==1);
  $('play-section').disabled=!selectionValid||!$('preview-player').readyState;
  updateTimeline();updateVertical();updateFrameInfo();
  $('video-source-note').hidden = current?.kind !== 'VOD';
  $('video-source-note').textContent = partial ? 'O Keep vai salvar apenas o trecho escolhido do VOD. Mantenha o app aberto até terminar.' : 'O VOD será baixado por completo. Gravações longas podem levar mais tempo e ocupar bastante espaço. Mantenha o Keep aberto até terminar.';
}
function plannedSections() {
 if($('download-scope').value!=='section')return null;
 if(selectedItems().length!==1)throw new Error('Selecione apenas um vídeo para editar os trechos.');
 const sections=editSections.length?editSections.map(item=>({...item})):[selectedSection()];
 if(!sections.length||sections.length>24)throw new Error('Escolha de 1 a 24 trechos.');
 for(const section of sections){if(!Number.isFinite(section.start)||!Number.isFinite(section.end)||section.start<0||section.end-section.start<.0999||section.end>Number(current?.duration)+.001)throw new Error('Escolha trechos de pelo menos 0,1 segundo dentro da duração do vídeo.');}
 const total=sections.reduce((sum,item)=>sum+item.end-item.start,0);
 if($('format').value==='gif'&&total>30.001)throw new Error('Para criar GIF, selecione no máximo 30 segundos no total.');
 return sections;
}
function configureSection(section) {
  const available = Number.isFinite(current.duration) && current.duration > 0;
  $('scope-section').disabled = !available || !session?.ffmpeg || selectedItems().length !== 1;
  $('download-scope').value = section && available && session?.ffmpeg ? 'section' : 'full';
  $('section-start').value = timeLabel(section?.start || 0);
  $('section-end').value=timeLabel(section?.end??(available?current.duration:0));
  updateSection();
}
function resetEditor() {
 editSections=[];previewFPS=null;previewDimensions=null;previewFrameTimes=null;previewFrameNote='';cropDrag=null;frameSeekBusy=false;
 $('circle-size').value='35';$('circle-x').value='5';$('circle-y').value='5';editorLayout='full';pendingCaptionCues=null;editorRegions={camera:{x:0,y:0,width:.28,height:.28},content:{x:.25,y:0,width:.5,height:1}};$('camera-height').value='30';$('region-select').value='camera';syncRegionFields();$('captions-enabled').checked=false;resetCaptionState();
 $('vertical-enabled').checked=false;$('crop-x').value='50';$('crop-y').value='50';$('section-output').value='separate';
 renderSections();
}
function renderSections() {
 $('section-list').innerHTML=editSections.map((section,index)=>`<li><span><strong>Trecho ${index+1}</strong> ${esc(timeLabel(section.start))} → ${esc(timeLabel(section.end))}<small>${esc(timeLabel(section.end-section.start))} de duração</small></span><div><button type="button" data-section-action="up" data-index="${index}" aria-label="Subir trecho ${index+1}"${index===0?' disabled':''}>↑</button><button type="button" data-section-action="down" data-index="${index}" aria-label="Descer trecho ${index+1}"${index===editSections.length-1?' disabled':''}>↓</button><button type="button" data-section-action="remove" data-index="${index}">Remover</button></div></li>`).join('');
 $('clear-sections').hidden=!editSections.length;
 $('section-list-note').textContent=editSections.length?'A exportação usa os trechos desta lista. Ajuste os horários e clique em Adicionar trecho para incluir outro.':'Sem lista: será baixado o intervalo dos campos Início e Fim. Adicione trechos para baixar vários ou montar uma sequência.';
}
$('add-section').addEventListener('click',()=>{
 try{const section=selectedSection();if(!section)return;if(editSections.length>=24)throw new Error('A lista pode ter no máximo 24 trechos.');editSections.push({...section});renderSections();updateSection();toast('Trecho adicionado à lista.');}
 catch(error){toast(error.message);}
});
$('clear-sections').addEventListener('click',()=>{editSections=[];renderSections();updateSection();});
$('section-list').addEventListener('click',event=>{
 const button=event.target.closest('[data-section-action]');if(!button)return;const index=Number(button.dataset.index),action=button.dataset.sectionAction;
 if(!Number.isInteger(index)||index<0||index>=editSections.length)return;
 if(action==='remove')editSections.splice(index,1);
 else {const next=index+(action==='up'?-1:1);if(next<0||next>=editSections.length)return;[editSections[index],editSections[next]]=[editSections[next],editSections[index]];}
 renderSections();updateSection();
});
$('section-output').addEventListener('change',updateSection);
function updateTimeline() {
 const duration=Number(current?.duration)||0;
 let start=0,end=duration;try{start=parseTime($('section-start').value);end=parseTime($('section-end').value);}catch{}
 start=Math.max(0,Math.min(duration,start));end=Math.max(start,Math.min(duration,end));
 for(const edge of ['start','end']){$('timeline-'+edge).max=String(duration);$('timeline-'+edge).disabled=!duration;$('timeline-'+edge).value=String(edge==='start'?start:end);}
 $('timeline-start-label').textContent=timeLabel(start);$('timeline-end-label').textContent=timeLabel(end);
 const left=duration?start/duration*1000:0,right=duration?end/duration*1000:1000;
 $('timeline-selection').setAttribute('x',String(left));$('timeline-selection').setAttribute('width',String(Math.max(0,right-left)));
 updateTimelineClock();
}
function updateTimelineClock() {
 const duration=Number(current?.duration)||1,x=Math.max(0,Math.min(1000,$('preview-player').currentTime/duration*1000||0));
 $('timeline-playhead').setAttribute('x1',String(x));$('timeline-playhead').setAttribute('x2',String(x));
}
['start','end'].forEach(edge=>$('timeline-'+edge).addEventListener('input',()=>{
 const duration=Number(current?.duration)||0;let start=Number($('timeline-start').value),end=Number($('timeline-end').value);
 if(edge==='start')start=Math.max(0,Math.min(start,end-.1));else end=Math.min(duration,Math.max(end,start+.1));
 $('section-start').value=timeLabel(start);$('section-end').value=timeLabel(end);previewPlayingSection=false;updateSection();
}));
function frameRate() {
 const preview=Number(previewFPS),source=Number(current?.fps);
 if(Number.isFinite(preview)&&preview>0&&preview<=240)return{fps:preview,estimated:false};
 if(Number.isFinite(source)&&source>0&&source<=240)return{fps:source,estimated:false};
 return{fps:30,estimated:true};
}
function updateFrameInfo() {
 const info=frameRate();$('frame-note').textContent=(previewFrameTimes?'Um quadro por vez, conforme os quadros reais da prévia.':'Passo estimado de 1/'+info.fps.toLocaleString('pt-BR',{maximumFractionDigits:3})+' s. '+(previewFrameNote||'A prévia não tem uma lista completa dos quadros; o intervalo pode variar.'))+' A prévia pausa ao avançar ou voltar.';
 for(const id of ['frame-back','frame-forward'])$(id).disabled=!$('preview-player').readyState||frameSeekBusy;
}
function validatedFrameTimes(values,duration) {
 if(!Array.isArray(values)||!values.length||values.length>500000)return null;
 let previous=-1;
 for(const value of values){if(typeof value!=='number'||!Number.isFinite(value)||value<0||value<=previous||(Number.isFinite(duration)&&value>duration+.25))return null;previous=value;}
 return values.slice();
}
function stepFrame(direction) {
 const player=$('preview-player');if(!player.readyState||frameSeekBusy)return;
 player.pause();previewPlayingSection=false;
 const fps=frameRate().fps,bound=Number.isFinite(player.duration)?player.duration:Number(current?.duration)||0;
 let target;
 if(previewFrameTimes){
   let low=0,high=previewFrameTimes.length-1,currentFrame=-1;
   while(low<=high){const middle=(low+high)>>1;if(previewFrameTimes[middle]<=player.currentTime+.000001){currentFrame=middle;low=middle+1;}else high=middle-1;}
   const index=Math.max(0,Math.min(previewFrameTimes.length-1,currentFrame+direction));
   const gap=(previewFrameTimes[index+1]??(previewFrameTimes[index]+1/fps))-previewFrameTimes[index];
   target=previewFrameTimes[index]+Math.min(.0001,gap/2);
 }else target=(Math.round(player.currentTime*fps)+direction)/fps;
 const next=Math.max(0,Math.min(Math.max(0,bound-.000001),target));
 if(Math.abs(next-player.currentTime)<.0001)return;
 frameSeekBusy=true;updateFrameInfo();player.currentTime=next;
 const generation=previewGeneration;setTimeout(()=>{if(generation===previewGeneration){frameSeekBusy=false;updateFrameInfo();}},2000);
}
$('frame-back').addEventListener('click',()=>stepFrame(-1));$('frame-forward').addEventListener('click',()=>stepFrame(1));
$('preview-player').addEventListener('seeked',()=>{frameSeekBusy=false;updateFrameInfo();updateTimelineClock();});
function videoDimensions() {
 const width=Number(current?.width)||Number(previewDimensions?.width)||$('preview-player').videoWidth;
 const height=Number(current?.height)||Number(previewDimensions?.height)||$('preview-player').videoHeight;
 return width>0&&height>0?{width,height}:null;
}
function alreadyVertical() {const dimensions=videoDimensions();return!!dimensions&&Math.abs(dimensions.width/dimensions.height-9/16)<.015;}
function verticalEnabled() {return $('format').value==='video'&&$('vertical-enabled').checked&&(editorLayout!=='full'||!alreadyVertical());}
function captionsEnabled() {return $('format').value==='video'&&$('captions-enabled').checked;}
function outputPreviewEnabled() {return verticalEnabled()||captionsEnabled();}
function updateVertical() {
 const dimensions=videoDimensions(),native=alreadyVertical(),video=$('format').value==='video',single=selectedItems().length===1;
 if(native&&editorLayout==='full')$('vertical-enabled').checked=false;
 $('vertical-enabled').disabled=!video||(native&&editorLayout==='full')||!single||!dimensions;
 $('vertical-layout-options').hidden=!video||!single||!dimensions;
 for(const layout of ['full','stacked','circle']){const button=$('vertical-layout-'+layout);button.classList.toggle('active',editorLayout===layout);button.setAttribute('aria-pressed',String(editorLayout===layout));}
 $('vertical-note').textContent=!video?'O enquadramento vertical está disponível para vídeo MP4.':native&&editorLayout==='full'?'Este vídeo já está em 9:16. O modo Cheia mantém seu enquadramento; você também pode escolher Empilhado.':!dimensions?'A plataforma não informou as dimensões. Prepare a prévia para conferir o enquadramento.':editorLayout==='circle'?'Selecione a câmera e o conteúdo. Ajuste o tamanho e a posição da câmera em Bola no resultado.':editorLayout==='stacked'?'Escolha a área da câmera e a do conteúdo no vídeo original. O resultado coloca a câmera acima do conteúdo.':'Ative para escolher uma área vertical da imagem. O restante da imagem será cortado.';
 const enabled=verticalEnabled(),stacked=enabled&&editorLayout!=='full';
 $('vertical-preview').style.touchAction=editorLayout==='circle'?'none':'';
 $('circle-controls').hidden=editorLayout!=='circle';$('stack-height-controls').hidden=editorLayout==='circle';
 for(const key of ['size','x','y'])$('circle-'+key+'-value').textContent=Math.round(Number($('circle-'+key).value))+'%';
 $('crop-controls').hidden=!enabled;$('full-crop-controls').hidden=stacked;$('stacked-crop-controls').hidden=!stacked;
 $('crop-overlay').toggleAttribute('hidden',!enabled||!$('preview-player').readyState);
 $('crop-frame').toggleAttribute('hidden',stacked);$('crop-drag-handle').toggleAttribute('hidden',stacked);$('crop-shade').toggleAttribute('hidden',stacked);$('stacked-regions').toggleAttribute('hidden',!stacked);
 $('crop-instructions').textContent=stacked?'Marque a câmera em rosa e o conteúdo em azul. Arraste as bordas e redimensione pelos quadrados; confira o resultado ao lado.':'Arraste a borda verde no original ou ajuste os controles para escolher a área que será mantida.';
 $('crop-x-value').textContent=Math.round(Number($('crop-x').value))+'%';$('crop-y-value').textContent=Math.round(Number($('crop-y').value))+'%';
 $('camera-height-value').textContent=Math.round(Number($('camera-height').value))+'%';
 $('vertical-preview-note').textContent=enabled?($('preview-player').readyState>=2?'Confira o resultado ao lado do original. A prévia acompanha a reprodução e os ajustes.':'Prepare a prévia abaixo para ver o resultado do enquadramento antes de baixar.'):'A prévia acompanha o mesmo instante do vídeo original.';
 const output=outputPreviewEnabled();
 $('preview-comparison').classList.toggle('vertical-active',enabled);$('preview-comparison').classList.toggle('captions-active',captionsEnabled());
 $('crop-source-label').hidden=!output;$('crop-source-label').textContent=stacked?'Original · câmera rosa e conteúdo azul':enabled?'Original · arraste a borda verde':'Original · sem legendas';
 $('vertical-output').hidden=!output;$('stack-divider').hidden=!enabled||editorLayout!=='stacked';
 $('stack-divider').style.top=String(Number($('camera-height').value))+'%';
 $('output-preview-heading').textContent=enabled?'Resultado · 9:16'+(editorLayout==='circle'?' · câmera em Bola':stacked?' empilhado':''):'Resultado · com legendas';
 renderCaptionState();updateCropOverlay();drawVerticalPreview();scheduleVerticalPreview();
}
function cropPixels(width,height,x,y) {
 // Match the 18 x 32 crop units and even pixel positions used by FFmpeg.
 const w=Math.floor(Math.min(width,height*9/16)/18)*18,h=Math.floor(Math.min(height,width*16/9)/32)*32;
 if(w<=0||h<=0)return null;
 const even=value=>Math.floor(Math.round(value)/2)*2;
 return{x:Math.max(0,Math.min(width-w,even((width-w)*x))),y:Math.max(0,Math.min(height-h,even((height-h)*y))),width:w,height:h};
}
function cropGeometry() {
 const player=$('preview-player'),rect=player.getBoundingClientRect(),width=player.videoWidth||videoDimensions()?.width,height=player.videoHeight||videoDimensions()?.height;
 if(!rect.width||!rect.height||!width||!height)return null;
 const scale=Math.min(rect.width/width,rect.height/height),image={x:(rect.width-width*scale)/2,y:(rect.height-height*scale)/2,width:width*scale,height:height*scale};
 const source=videoDimensions(),crop=source&&cropPixels(source.width,source.height,Number($('crop-x').value)/100,Number($('crop-y').value)/100);
 if(!crop)return null;
 return{rect,image,frame:{x:image.x+image.width*crop.x/source.width,y:image.y+image.height*crop.y/source.height,width:image.width*crop.width/source.width,height:image.height*crop.height/source.height}};
}
function circleGeometry(width,height,camera,content,size,position){
 const even=v=>2*Math.floor(v/2),unit=Math.floor(Math.min(width,1080)/18),w=18*unit,h=32*unit,d=even(w*size);
 if(unit<1||height<2)return null;
 const cover=(r,pw,ph)=>{
  const x=even(r.x*width),y=even(r.y*height),rw=Math.min(even(r.width*width),even(width-x)),rh=Math.min(even(r.height*height),even(height-y));
  if(rw<2||rh<2)return null;
  let cw=rw,ch=rh;if(rw/rh>pw/ph)cw=even(rh*pw/ph);else ch=even(rw*ph/pw);
  return cw<2||ch<2?null:{x:x+even((rw-cw)/2),y:y+even((rh-ch)/2),width:cw,height:ch};
 };
 const a=cover(camera,d,d),b=cover(content,w,h);if(!a||!b)return null;
 return{width:w,height:h,diameter:d,left:even((w-d)*position.x),top:even((h-d)*position.y),camera:a,content:b};
}
function currentCircleGeometry(){const d=videoDimensions();return d&&circleGeometry(d.width,d.height,editorRegions.camera,editorRegions.content,Number($('circle-size').value)/100,{x:Number($('circle-x').value)/100,y:Number($('circle-y').value)/100});}

function stackedGeometry(width,height,camera,content,share) {
 const even=value=>2*Math.floor(value/2),unit=Math.floor(Math.min(width,1080)/18),w=18*unit,h=32*unit;
 if(unit<1)return null;
 const cameraHeight=even(h*share),contentHeight=h-cameraHeight;
 const cover=(region,paneHeight)=>{
  const left=even(region.x*width),top=even(region.y*height),rw=Math.min(even(region.width*width),even(width-left)),rh=Math.min(even(region.height*height),even(height-top));
  let cw=rw,ch=rh;if(rw/rh>w/paneHeight)cw=even(rh*w/paneHeight);else ch=even(rw*paneHeight/w);
  if(cw<2||ch<2)return null;
  return{x:left+even((rw-cw)/2),y:top+even((rh-ch)/2),width:cw,height:ch};
 };
 const a=cover(camera,cameraHeight),b=cover(content,contentHeight);if(!a||!b)return null;
 return{width:w,height:h,camera_height:cameraHeight,content_height:contentHeight,camera:a,content:b};
}
function currentStackGeometry() {const d=videoDimensions();return d&&stackedGeometry(d.width,d.height,editorRegions.camera,editorRegions.content,Number($('camera-height').value)/100);}
function syncRegionFields() {const region=editorRegions[$('region-select').value];for(const key of ['x','y','width','height'])$('region-'+key).value=String(Math.round(region[key]*1000)/10);}
function normalizeRegion(region) {
 const width=Math.max(.05,Math.min(1,region.width)),height=Math.max(.05,Math.min(1,region.height));
 return{x:Math.max(0,Math.min(1-width,region.x)),y:Math.max(0,Math.min(1-height,region.y)),width,height};
}
function stopVerticalPreview() {
 const player=$('preview-player');
 if(cropFrameCallback!==null&&player.cancelVideoFrameCallback)player.cancelVideoFrameCallback(cropFrameCallback);
 if(cropAnimationFrame!==null)cancelAnimationFrame(cropAnimationFrame);
 cropFrameCallback=cropAnimationFrame=null;
}
function clearVerticalPreview() {
 stopVerticalPreview();const canvas=$('vertical-preview'),context=canvas.getContext('2d');
 if(context)context.clearRect(0,0,canvas.width,canvas.height);
 canvas.hidden=true;$('vertical-preview-empty').hidden=false;$('vertical-preview-empty').textContent='Aguardando a imagem da prévia…';
}
function wrapCaptionText(text) {
 const words=String(text).trim().replace(/\s+/g,' ').split(' ').filter(Boolean),lines=[];let line='';
 for(const word of words){if(line&&Array.from(line+' '+word).length>32){lines.push(line);line=word;}else line+=(line?' ':'')+word;}
 if(line)lines.push(line);
 return lines.length>2?[lines[0],lines.slice(1).join(' ')]:lines;
}
function drawPreviewCaptions(context,width,height,time) {
 if(!captionsEnabled()||captionState!=='ready'||captionBinding!==previewId)return;
 const cue=captionCues.find(item=>time>=item.start&&time<item.end);if(!cue||!cue.text.trim())return;
 const lines=wrapCaptionText(cue.text),scale=Math.min(1,32/Math.max(1,...lines.map(line=>Array.from(line).length))),font=width*.052*scale;
 context.save();context.font='bold '+font+'px Arial, "Liberation Sans", sans-serif';context.textAlign='center';context.textBaseline='bottom';context.lineJoin='round';context.strokeStyle='#000';context.fillStyle='#fff';context.lineWidth=font*.09;
 lines.forEach((line,index)=>{const y=height*.92-(lines.length-1-index)*font*1.2;context.strokeText(line,width/2,y);context.fillText(line,width/2,y);});context.restore();
}
function drawVerticalPreview() {
 const player=$('preview-player'),canvas=$('vertical-preview'),source=videoDimensions();
 if(!outputPreviewEnabled()||$('preview-player-wrap').hidden||player.readyState<2||!player.videoWidth||!player.videoHeight||!source){clearVerticalPreview();return;}
 const vertical=verticalEnabled(),circular=vertical&&editorLayout==='circle',stacked=vertical&&editorLayout==='stacked',geometry=circular?currentCircleGeometry():stacked?currentStackGeometry():null;
 const crop=vertical&&!stacked&&!circular?cropPixels(source.width,source.height,Number($('crop-x').value)/100,Number($('crop-y').value)/100):{x:0,y:0,width:source.width,height:source.height};
 if(!crop||((stacked||circular)&&!geometry)){clearVerticalPreview();$('vertical-preview-empty').textContent='A área escolhida é pequena demais. Aumente o enquadramento.';return;}
 const width=360,height=vertical?640:Math.max(2,Math.round(width*source.height/source.width));
 if(canvas.width!==width||canvas.height!==height){canvas.width=width;canvas.height=height;}
 $('output-preview-screen').style.aspectRatio=String(width)+' / '+String(height);
 const context=canvas.getContext('2d');if(!context)return;
 const draw=(r,x,y,w,h)=>context.drawImage(player,r.x/source.width*player.videoWidth,r.y/source.height*player.videoHeight,r.width/source.width*player.videoWidth,r.height/source.height*player.videoHeight,x,y,w,h);
 try{
  if(circular){
   draw(geometry.content,0,0,width,height);const scale=width/geometry.width,d=geometry.diameter*scale,x=geometry.left*scale,y=geometry.top*scale;
   context.save();context.beginPath();context.arc(x+d/2,y+d/2,d/2,0,Math.PI*2);context.clip();draw(geometry.camera,x,y,d,d);context.restore();
  }else if(stacked){const top=height*geometry.camera_height/geometry.height;draw(geometry.camera,0,0,width,top);draw(geometry.content,0,top,width,height-top);}else draw(crop,0,0,width,height);
  drawPreviewCaptions(context,width,height,player.currentTime);canvas.hidden=false;$('vertical-preview-empty').hidden=true;
 }catch{clearVerticalPreview();$('vertical-preview-empty').textContent='Não foi possível desenhar este quadro. Prepare a prévia novamente.';}
}
// A paused load/seek still needs one decoded compositor frame, never an idle loop.
function scheduleVerticalPreview(force=false) {
 const player=$('preview-player');
 if(!outputPreviewEnabled()||(!force&&player.paused)||player.ended||$('preview-player-wrap').hidden){stopVerticalPreview();return;}
 if(cropFrameCallback!==null||cropAnimationFrame!==null)return;
 const next=()=>{cropFrameCallback=cropAnimationFrame=null;drawVerticalPreview();scheduleVerticalPreview();};
 if(player.requestVideoFrameCallback)cropFrameCallback=player.requestVideoFrameCallback(next);else cropAnimationFrame=requestAnimationFrame(next);
}
function updateCropOverlay() {
 if($('crop-overlay').hasAttribute('hidden'))return;
 const data=cropGeometry();if(!data)return;const{rect,image,frame}=data;
 $('crop-overlay').setAttribute('viewBox',`0 0 ${rect.width} ${rect.height}`);
 if(editorLayout!=='full'){
  for(const name of ['camera','content']){
   const r=editorRegions[name],f={x:image.x+image.width*r.x,y:image.y+image.height*r.y,width:image.width*r.width,height:image.height*r.height};
   for(const suffix of ['frame','drag-handle'])for(const key of ['x','y','width','height'])$(name+'-'+suffix).setAttribute(key,String(f[key]));
   $(name+'-resize-handle').setAttribute('x',String(f.x+f.width-6));$(name+'-resize-handle').setAttribute('y',String(f.y+f.height-6));
   $(name+'-label').setAttribute('x',String(f.x+5));$(name+'-label').setAttribute('y',String(f.y+17));
  }
 }else{
  $('crop-shade').setAttribute('d',`M${image.x},${image.y}h${image.width}v${image.height}h-${image.width}Z M${frame.x},${frame.y}h${frame.width}v${frame.height}h-${frame.width}Z`);
  for(const id of ['crop-frame','crop-drag-handle'])for(const key of ['x','y','width','height'])$(id).setAttribute(key,String(frame[key]));
 }
}
$('vertical-enabled').addEventListener('change',updateSection);
for(const layout of ['full','stacked','circle'])$('vertical-layout-'+layout).addEventListener('click',()=>{editorLayout=layout;$('vertical-enabled').checked=true;syncRegionFields();updateSection();});
['crop-x','crop-y'].forEach(id=>$(id).addEventListener('input',updateVertical));
$('center-crop').addEventListener('click',()=>{$('crop-x').value='50';$('crop-y').value='50';updateVertical();});
$('region-select').addEventListener('change',syncRegionFields);
for(const key of ['x','y','width','height'])$('region-'+key).addEventListener('input',()=>{
 if(!$('region-'+key).value.trim())return;const value=Number($('region-'+key).value);if(!Number.isFinite(value))return;
 const name=$('region-select').value;editorRegions[name]=normalizeRegion({...editorRegions[name],[key]:value/100});updateVertical();
});
for(const key of ['x','y','width','height'])$('region-'+key).addEventListener('change',syncRegionFields);
$('camera-height').addEventListener('input',updateVertical);
$('crop-drag-handle').addEventListener('pointerdown',event=>{
 if(!verticalEnabled()||editorLayout!=='full')return;const data=cropGeometry();if(!data)return;event.preventDefault();
 cropDrag={pointerId:event.pointerId,clientX:event.clientX,clientY:event.clientY,x:Number($('crop-x').value)/100,y:Number($('crop-y').value)/100,travelX:data.image.width-data.frame.width,travelY:data.image.height-data.frame.height};event.target.setPointerCapture(event.pointerId);
});
$('crop-drag-handle').addEventListener('pointermove',event=>{
 if(!cropDrag||cropDrag.region||event.pointerId!==cropDrag.pointerId)return;event.preventDefault();const clamp=value=>Math.max(0,Math.min(1,value));
 $('crop-x').value=String(clamp(cropDrag.x+(cropDrag.travelX?(event.clientX-cropDrag.clientX)/cropDrag.travelX:0))*100);$('crop-y').value=String(clamp(cropDrag.y+(cropDrag.travelY?(event.clientY-cropDrag.clientY)/cropDrag.travelY:0))*100);updateVertical();
});
for(const name of ['camera','content'])for(const mode of ['drag','resize']){
 const handle=$(name+'-'+(mode==='drag'?'drag-handle':'resize-handle'));
 handle.addEventListener('pointerdown',event=>{
  if(!verticalEnabled()||editorLayout==='full')return;const data=cropGeometry();if(!data)return;event.preventDefault();event.stopPropagation();$('region-select').value=name;syncRegionFields();
  cropDrag={pointerId:event.pointerId,region:name,mode,clientX:event.clientX,clientY:event.clientY,initial:{...editorRegions[name]},image:data.image};handle.setPointerCapture(event.pointerId);
 });
 handle.addEventListener('pointermove',event=>{
  if(!cropDrag||cropDrag.region!==name||event.pointerId!==cropDrag.pointerId)return;event.preventDefault();
  const dx=(event.clientX-cropDrag.clientX)/cropDrag.image.width,dy=(event.clientY-cropDrag.clientY)/cropDrag.image.height,r=cropDrag.initial;
  editorRegions[name]=normalizeRegion(mode==='resize'?{...r,width:Math.max(.05,Math.min(1-r.x,r.width+dx)),height:Math.max(.05,Math.min(1-r.y,r.height+dy))}:{...r,x:r.x+dx,y:r.y+dy});syncRegionFields();updateVertical();
 });
 for(const type of ['pointerup','pointercancel','lostpointercapture'])handle.addEventListener(type,()=>{cropDrag=null;});
}
for(const type of ['pointerup','pointercancel','lostpointercapture'])$('crop-drag-handle').addEventListener(type,()=>{cropDrag=null;});
let dividerDrag=null;
$('stack-divider').addEventListener('pointerdown',event=>{event.preventDefault();dividerDrag=event.pointerId;$('stack-divider').setPointerCapture(event.pointerId);});
$('stack-divider').addEventListener('pointermove',event=>{if(dividerDrag!==event.pointerId)return;const box=$('output-preview-screen').getBoundingClientRect();$('camera-height').value=String(Math.max(15,Math.min(60,(event.clientY-box.top)/box.height*100)));updateVertical();});
for(const type of ['pointerup','pointercancel','lostpointercapture'])$('stack-divider').addEventListener(type,()=>{dividerDrag=null;});
$('stack-divider').addEventListener('keydown',event=>{if(!['ArrowUp','ArrowDown'].includes(event.key))return;event.preventDefault();$('camera-height').value=String(Math.max(15,Math.min(60,Number($('camera-height').value)+(event.key==='ArrowDown'?1:-1))));updateVertical();});
window.addEventListener('resize',updateCropOverlay);
for(const event of ['loadeddata','seeked','timeupdate','pause','ended'])$('preview-player').addEventListener(event,()=>{drawVerticalPreview();scheduleVerticalPreview(event==='loadeddata'||event==='seeked');renderCaptionState();});
$('preview-player').addEventListener('play',()=>scheduleVerticalPreview());
for(const event of ['emptied','error'])$('preview-player').addEventListener(event,clearVerticalPreview);
function validCaptionCues(cues,duration=Number(current?.duration)) {
 if(!Array.isArray(cues)||!Number.isFinite(duration)||duration<=0)throw new Error('A lista de legendas está inválida. Gere novamente.');
 let end=0,total=0;return cues.map(item=>{
  const start=item?.start,next=item?.end,text=typeof item?.text==='string'?item.text.trim():'';total+=Array.from(text).length;
  if(typeof start!=='number'||typeof next!=='number'||!Number.isFinite(start)||!Number.isFinite(next)||start<end-.001||start<0||next<=start||next>duration+.01||!text||Array.from(text).length>180)throw new Error('Confira o texto e os horários das legendas antes de baixar.');
  if(Math.round(next*1000)<=Math.round(start*1000))throw new Error('Uma legenda ficou curta demais. Gere novamente para conferir os horários.');
  end=next;return{start,end:next,text};
 });
}
function normalizeGeneratedCaptionCues(cues,duration=Number(current?.duration)) {
 if(!Array.isArray(cues)||!Number.isFinite(duration)||duration<=0)throw new Error('A lista de legendas está inválida. Gere novamente.');
 let previous=0,total=0,clamped=0,removed=0;const normalized=[];
 for(const item of cues){
  const start=item?.start,end=item?.end,text=typeof item?.text==='string'?item.text.trim():'';total+=Array.from(text).length;
  if(typeof start!=='number'||typeof end!=='number'||!Number.isFinite(start)||!Number.isFinite(end)||start<previous-.001||start<0||end<=start||!text||Array.from(text).length>180)throw new Error('Confira o texto e os horários das legendas antes de baixar.');
  previous=end;
  if(start>=duration){removed++;continue;}
  if(end>duration+.25)throw new Error('Os horários das legendas não correspondem à duração do vídeo. Prepare a prévia e gere novamente.');
  const next=Math.min(end,duration);if(next<end)clamped++;
  if(Math.round(next*1000)<=Math.round(start*1000))throw new Error('Uma legenda ficou curta demais ao ajustar o fim do vídeo. Gere novamente.');
  normalized.push({start,end:next,text});
 }
 return{cues:validCaptionCues(normalized,duration),clamped,removed};
}
function captionExportReady() {
 if(!captionsEnabled())return true;
 if(captionState!=='ready'||captionBinding!==previewId)throw new Error('Gere e confira as legendas desta prévia antes de baixar.');
 if(!captionCues.length)throw new Error('Nenhuma fala foi identificada. Gere novamente ou desative as legendas para baixar.');
 if(captionSections){const selected=plannedSections()||[{start:0,end:current.duration}];if(selected.some(part=>!captionSections.some(known=>part.start>=known.start-.01&&part.end<=known.end+.01)))throw new Error('A seleção mudou. Gere as legendas dos novos trechos antes de baixar.');}
 validCaptionCues(captionCues);return true;
}
function captionBusy() {return ['queued','preparing','transcribing'].includes(captionState);}
function renderCaptionState() {
 const busy=captionBusy(),ready=!!previewId&&$('preview-player').readyState>=2&&!$('preview-player-wrap').hidden;
 const allowed=$('format').value==='video'&&selectedItems().length===1&&Number(current?.duration)>0;
 $('captions-enabled').disabled=!allowed;$('captions-controls').hidden=!$('captions-enabled').checked;
 $('generate-captions').disabled=!allowed||!ready||busy||!token||keepClosed;
 $('generate-captions').textContent=$('download-scope').value==='section'?'Gerar legendas dos trechos':'Gerar legendas do vídeo inteiro';$('cancel-captions').hidden=!busy;$('captions-progress').hidden=!busy;
 if(captionState==='idle')$('captions-status').textContent=ready?'Clique em Gerar legendas para reconhecer a fala e conferir o resultado na prévia.':'Prepare uma prévia para gerar as legendas.';
}
function resetCaptionState(cancel=true) {
 captionSections=null;const previous=captionTask;captionTask='';captionGeneration++;clearTimeout(captionTimer);captionState='idle';captionCues=[];captionBinding='';$('captions-review').hidden=true;$('captions-list').replaceChildren();$('captions-status').classList.remove('invalid');
 if(cancel&&previous&&token)api('subtitles/cancel',{id:previous}).catch(()=>{});
 renderCaptionState();
}
function renderCaptionReview() {
 $('captions-review').hidden=!captionCues.length;
 const nodes=captionCues.map((cue,index)=>{
  const item=document.createElement('li'),time=document.createElement('span'),input=document.createElement('textarea'),field=document.createElement('div'),hint=document.createElement('small');time.textContent=timeLabel(cue.start)+' → '+timeLabel(cue.end);input.value=cue.text;input.maxLength=180;input.rows=2;input.setAttribute('aria-label','Texto da legenda '+(index+1));
  hint.textContent='Texto longo: encurte a frase para melhorar a leitura.';hint.className='caption-length-note';hint.hidden=Array.from(cue.text).length<=64;
  input.addEventListener('input',()=>{captionCues[index].text=input.value;input.setAttribute('aria-invalid',String(!input.value.trim()));hint.hidden=Array.from(input.value).length<=64;drawVerticalPreview();updateSection();});field.append(input,hint);item.append(time,field);return item;
 });$('captions-list').replaceChildren(...nodes);
}
function acceptCaptionTask(data,binding,generation) {
 if(generation!==captionGeneration||binding!==previewId||keepClosed){if(data.id&&token)api('subtitles/cancel',{id:data.id}).catch(()=>{});return;}
 if(data.preview_id&&data.preview_id!==binding)throw new Error('As legendas não correspondem a esta prévia. Gere novamente.');
 if(data.id)captionTask=data.id;captionState=data.status||data.state||'preparing';
 if(captionState==='transcribing'&&Number.isFinite(data.progress))$('captions-progress').value=Math.max(0,Math.min(100,data.progress));else $('captions-progress').removeAttribute('value');
 $('captions-status').textContent=data.error||data.stage||data.message||'Reconhecendo a fala…';$('captions-status').classList.toggle('invalid',captionState==='failed'||!!data.error);
 if(captionState==='ready'){
  const normalized=normalizeGeneratedCaptionCues(data.cues||[]);captionCues=normalized.cues;captionBinding=binding;renderCaptionReview();
  const note=captionCues.length?captionCues.length+' legendas geradas. Confira a prévia e revise o texto; prefira frases curtas para facilitar a leitura.':normalized.removed?'As legendas recebidas estavam fora da duração deste vídeo. Gere novamente.':'Nenhuma fala foi identificada nesta prévia.';
  const warnings=[];if(normalized.clamped)warnings.push('O fim de '+normalized.clamped+' legenda(s) foi ajustado ao fim do vídeo.');if(normalized.removed)warnings.push(normalized.removed+' legenda(s) fora da duração foram ignoradas. Confira a prévia.');
  $('captions-status').textContent=[note,...warnings].join(' ');updateSection();
 }else if(captionBusy()){
  captionTimer=setTimeout(async()=>{try{acceptCaptionTask(await api('subtitles?id='+encodeURIComponent(captionTask)),binding,generation);}catch(error){captionFailure(error,generation);}},1400);
 }
 renderCaptionState();drawVerticalPreview();
}
function captionFailure(error,generation) {if(generation!==captionGeneration)return;captionState='failed';$('captions-status').textContent=error.message;$('captions-status').classList.add('invalid');renderCaptionState();updateSection();}
$('captions-enabled').addEventListener('change',updateSection);
$('captions-language').addEventListener('change',()=>{resetCaptionState();updateSection();});
$('generate-captions').addEventListener('click',async()=>{
 if(!previewId||!token||captionBusy())return;
 resetCaptionState();const generation=captionGeneration,binding=previewId;captionState='preparing';$('captions-status').textContent='Preparando o reconhecimento de fala…';renderCaptionState();updateSection();
 try{const sections=plannedSections();captionSections=sections?sections.map(part=>({...part})).sort((a,b)=>a.start-b.start).reduce((known,part)=>{if(known.length&&part.start<=known[known.length-1].end)known[known.length-1].end=Math.max(known[known.length-1].end,part.end);else known.push(part);return known;},[]):null;const key=new URL($('preview-player').src,location.href).searchParams.get('key');acceptCaptionTask(await api('subtitles',{preview_id:binding,key,language:$('captions-language').value,sections}),binding,generation);}catch(error){captionFailure(error,generation);}
});
$('cancel-captions').addEventListener('click',async()=>{
 const id=captionTask;resetCaptionState(false);captionState='cancelled';$('captions-status').textContent='Geração cancelada. Você pode gerar novamente ou desativar as legendas.';renderCaptionState();updateSection();if(id&&token)try{await api('subtitles/cancel',{id});}catch(error){$('captions-status').textContent=error.message;}
});
async function api(path, body) {
  const response=await fetch('/api/'+path, {method:body?'POST':'GET',headers:{'X-Keep-Token':token,...(body?{'Content-Type':'application/json'}:{})},...(body?{body:JSON.stringify(body)}:{})});
  const data=await response.json();
  if(!response.ok){const error=new Error(data.error || 'Não foi possível conectar ao Keep.');error.status=response.status;error.data=data;throw error;}
  return data;
}
function view(name) {
  if(!['new','library','settings'].includes(name)) name='new';
  ['new','library','settings'].forEach(v=>$(v+'-view').hidden=v!==name);
  document.querySelectorAll('[data-view]').forEach(el=>el.classList.toggle('active',el.dataset.view===name));
  $('page-name').textContent={new:'Novo download',library:'Meus downloads',settings:'Preferências'}[name];
  history.replaceState(null,'','#'+name);
  if(name==='library')renderJobs();
  if(name==='library'||name==='settings')refreshStorage(true);
}
document.querySelectorAll('[data-view]').forEach(el=>el.addEventListener('click',()=>view(el.dataset.view)));
document.querySelector('.brand').addEventListener('click',()=>view('new'));
$('view-all').addEventListener('click',()=>view('library'));
$('help').addEventListener('click',()=>$('help-dialog').showModal());
document.querySelector('.close-dialog').addEventListener('click',()=>$('help-dialog').close());
$('help-dialog').addEventListener('click',event=>{if(event.target===$('help-dialog')){const r=event.target.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)event.target.close();}});
async function pasteLink(download = false) {
  if(searching || startingDownload || batchRunning) return;
  try {
    $('video-url').value=(await navigator.clipboard.readText()).trim();
    $('video-url').dispatchEvent(new Event('input'));$('video-url').focus();
    if(download) {
      if(!$('video-url').reportValidity())return;
      await inspectVideo(true);
    }
  } catch {toast('O navegador não permitiu colar. Clique no campo do link e pressione Ctrl + V. Depois use Buscar vídeo.');$('video-url').focus();}
}
$('paste').addEventListener('click',()=>pasteLink());
$('paste-download').addEventListener('click',()=>pasteLink(true));
$('video-url').addEventListener('input',()=>{invalidateVideo();message('');});
$('link-form').addEventListener('submit',e=>{e.preventDefault();inspectVideo();});
function selectedItems() {
  return currentItems.filter(item => selectedVideos.has(String(item.video_index)));
}
function videoRequest(item = current) {
  return {url:inspection?.url || item?.url || $('video-url').value.trim(), browser:$('browser-session').value,
    ...(item?.local_file?{local_file:true}:{}),...(item?.video_index !== undefined ? {video_index:item.video_index} : {}),
    ...(item?.video_id ? {video_id:item.video_id} : {}),
    ...(inspection?.inspection_id ? {inspection_id:inspection.inspection_id} : {}),...(item?.clip?{clip:{...item.clip}}:{})};
}
function showCurrentVideo(item, section = null) {
  resetPreview();resetEditor();current=item;
  $('video-title').textContent=item.title;
  $('video-platform').textContent=[item.platform,item.kind,currentItems.length>1?`Vídeo ${item.video_index}`:null].filter(Boolean).join(' · ');
  $('video-meta').textContent=[item.creator,Number(item.duration)>0?timeLabel(item.duration):null,item.heights?.length?`Original até ${item.heights[0]}p`:null].filter(Boolean).join(' · ');
  $('thumbnail').replaceChildren();
  if(item.thumbnail && /^https:\/\//.test(item.thumbnail)){const image=new Image();image.alt='Miniatura do vídeo';image.src=item.thumbnail;image.referrerPolicy='no-referrer';image.onerror=()=>{$('thumbnail').innerHTML=icon('play');};$('thumbnail').append(image);}else $('thumbnail').innerHTML=icon('play');
  configureQuality(item.heights);configureSection(section);
}
function renderPostVideos() {
  const multiple=currentItems.length>1;
  $('post-videos').hidden=!multiple;
  if(!multiple)return;
  $('post-video-list').innerHTML=currentItems.map(item=>`<label class="post-video-option${selectedVideos.has(String(item.video_index))?' selected':''}"><input type="checkbox" data-video-index="${esc(item.video_index)}"${selectedVideos.has(String(item.video_index))?' checked':''}><span class="post-video-thumb">${item.thumbnail&&/^https:\/\//.test(item.thumbnail)?`<img src="${esc(item.thumbnail)}" alt="" loading="lazy" referrerpolicy="no-referrer">`:icon('play')}</span><span><strong>Vídeo ${esc(item.video_index)}</strong><span>${esc(item.title)}</span><small>${Number(item.duration)>0?esc(timeLabel(item.duration)):'Duração não informada'}</small></span></label>`).join('');
  const count=selectedItems().length;
  $('post-video-note').textContent=count?`${count} de ${currentItems.length} selecionado${count===1?'':'s'}. Para escolher um trecho, selecione apenas um.`:'Marque os vídeos que deseja baixar. Nenhum vídeo será baixado até você escolher.';
  $('select-all-videos').textContent=count===currentItems.length?'Desmarcar todos':'Selecionar todos';
}
function changeSelectedVideos(index, checked) {
  if(checked)selectedVideos.add(String(index));else selectedVideos.delete(String(index));
  const chosen=selectedItems();
  if(chosen.length===1)showCurrentVideo(chosen[0]);
  else {
    resetPreview();resetEditor();
    configureSection(null);
  }
  renderPostVideos();updateSection();
}
$('post-video-list').addEventListener('change',event=>{if(event.target.matches('[data-video-index]'))changeSelectedVideos(event.target.dataset.videoIndex,event.target.checked);});
$('select-all-videos').addEventListener('click',()=>{
  const all=selectedItems().length===currentItems.length;
  selectedVideos=new Set(all?[]:currentItems.map(item=>String(item.video_index)));
  resetPreview();resetEditor();configureSection(null);renderPostVideos();updateSection();
});
let liveCandidate=null,liveId='',liveState=null,liveTimer,liveBusy=false,liveClipping=false;
let liveGeneration=0;
function detachLive(){
 liveGeneration++;const id=liveId;liveId='';liveCandidate=null;liveState=null;liveBusy=false;
 clearTimeout(liveTimer);$('live-panel').hidden=true;
 if(id&&token)api('live/stop',{id}).catch(()=>{});
}
function showLiveCandidate(data){
 if(liveId&&['connecting','buffering'].includes(liveState?.status)){
  message('O Keep já acompanha uma live. Pare o acompanhamento antes de escolher outra.',true);return false;
 }
 liveCandidate=data;$('live-panel').hidden=false;$('live-title').textContent='Ao vivo · '+data.title;
 renderLive(liveState);return true;
}
function renderLive(data){
 liveState=data;
 const active=!!data&&['connecting','buffering'].includes(data.status);
 $('live-start').hidden=active;$('live-start').disabled=liveBusy||liveClipping||!liveCandidate;
 $('live-stop').hidden=!active;$('live-stop').disabled=liveBusy;
 $('live-clip').disabled=liveClipping||liveBusy||!(data?.buffer_seconds>0);
 $('live-clip').textContent=liveClipping?'Preparando clipe…':'Salvar clipe e abrir prévia';
 $('live-status').textContent=data?(data.error||data.stage)+(data.buffer_seconds>0?` ${data.buffer_seconds} s recentes disponíveis.`:''):'Clique em Acompanhar live antes do momento que deseja guardar.';
 $('live-status').classList.toggle('invalid',data?.status==='failed');
}
function pollLive(){
 clearTimeout(liveTimer);const id=liveId;if(!id||keepClosed)return;
 liveTimer=setTimeout(async()=>{
  try{const data=await api('live?id='+encodeURIComponent(id));if(id!==liveId)return;renderLive(data);if(['connecting','buffering'].includes(data.status))pollLive();}
  catch(error){if(id===liveId){$('live-status').textContent=error.message;$('live-status').classList.add('invalid');$('live-clip').disabled=true;$('live-stop').hidden=false;if(error.status===404){liveId='';renderLive(null);$('live-status').textContent=error.message;}else pollLive();}}
 },1500);
}
$('live-start').addEventListener('click',async()=>{
 if(!liveCandidate||liveBusy)return;
 const generation=liveGeneration;liveBusy=true;renderLive(liveState);$('live-status').textContent='Conferindo a transmissão e iniciando o acompanhamento…';
 try{const data=await api('live/start',{url:liveCandidate.url,browser:$('browser-session').value});if(generation!==liveGeneration){api('live/stop',{id:data.id}).catch(()=>{});return;}liveId=data.id;renderLive(data);pollLive();}
 catch(error){if(generation===liveGeneration){$('live-status').textContent=error.message;$('live-status').classList.add('invalid');}}
 finally{if(generation===liveGeneration){liveBusy=false;$('live-start').disabled=false;$('live-stop').disabled=false;}}
});
$('live-stop').addEventListener('click',async()=>{
 if(!liveId||liveBusy)return;
 const generation=liveGeneration,id=liveId;
 liveBusy=true;clearTimeout(liveTimer);renderLive(liveState);
 try{const data=await api('live/stop',{id});if(generation===liveGeneration)renderLive(data);}
 catch(error){if(generation===liveGeneration){$('live-status').textContent=error.message;pollLive();}}
 finally{if(generation===liveGeneration){liveBusy=false;renderLive(liveState);}}
});
$('live-clip').addEventListener('click',async()=>{
 if(!liveId||liveClipping||liveBusy)return;
 liveClipping=true;renderLive(liveState);const generation=previewGeneration;
 try{
  const data=await api('live/clip',{id:liveId,seconds:Number($('live-seconds').value)});
  if(generation!==previewGeneration){if(data.preview?.id)api('preview/cancel',{id:data.preview.id}).catch(()=>{});toast('Clipe salvo em Meus downloads.');return;}
  inspection=null;currentItems=[{...data.video,clip:{id:data.preview.id}}];selectedVideos=new Set(['1']);
  showCurrentVideo(currentItems[0]);renderPostVideos();$('video-result').hidden=false;
  showPreview(data.preview,previewGeneration);$('prepare-preview').disabled=true;$('cancel-preview').hidden=true;
  message('Clipe original salvo em Meus downloads. Use a prévia abaixo para cortar, compor a câmera e gerar legendas; Baixar salva outra versão com suas edições.');
  await refresh();refreshStorage(true);
 }catch(error){$('live-status').textContent=error.message;$('live-status').classList.add('invalid');}
 finally{liveClipping=false;$('live-clip').textContent='Salvar clipe e abrir prévia';$('live-clip').disabled=!(liveState?.buffer_seconds>0);}
});

async function inspectVideo(automatic = false) {
  if(searching || startingDownload || batchRunning)return;
  if(!local){message('Para baixar este vídeo, baixe o app para seu sistema acima e abra o Keep no seu computador.');return;}
  if(!token){message('O Keep está desconectado. Reinicie o app e atualize a página.',true);return;}
  searching=true;$('cookie-file').disabled=true;$('remove-cookies').disabled=true;invalidateVideo();$('inspect').disabled=true;$('paste-download').disabled=true;$('inspect').textContent='Buscando vídeo…';message('Verificando o vídeo e o áudio disponíveis. Isso pode levar alguns instantes.');
  const submitted=$('video-url').value.trim(), submittedBrowser=$('browser-session').value;
  const requestedSection=retrySection,requestedEditor=retryEditor,requestedOptions=retryDownloadOptions;retrySection=null;retryEditor=null;retryDownloadOptions=null;
  try{
    const data=await api('inspect',{url:submitted,browser:submittedBrowser});
    if($('video-url').value.trim()!==submitted || $('browser-session').value!==submittedBrowser){message('O link ou a opção de cookies mudou. Busque o vídeo novamente.');return;}
    if(data.live){if(showLiveCandidate(data))message('Live encontrada. Inicie o acompanhamento para salvar os próximos momentos.');return;}
    inspection=data;
    currentItems=(Array.isArray(data.items)&&data.items.length?data.items:[data]).map((item,index)=>({...data,...item,items:undefined,video_index:item.video_index??index+1}));
    selectedVideos=new Set(currentItems.length===1?[String(currentItems[0].video_index)]:[]);
    showCurrentVideo(currentItems[0],automatic?null:requestedSection);renderPostVideos();$('video-result').hidden=false;message('');
    if(!automatic&&requestedOptions){
      $('format').value=requestedOptions.mode||'video';$('custom-name').value=requestedOptions.custom_name||'';
      const option=Array.from($('quality').options).find(item=>item.value===requestedOptions.quality&&!item.disabled);
      if(option)$('quality').value=option.value;
      updateSection();
    }
    if(!automatic&&requestedEditor&&selectedItems().length===1){
      $('format').value=requestedEditor.mode;editSections=requestedEditor.sections.map(item=>({...item}));$('download-scope').value='section';$('section-output').value='join';
      $('section-start').value=timeLabel(editSections[0].start);$('section-end').value=timeLabel(editSections[0].end);$('vertical-enabled').checked=requestedEditor.vertical===true;
      $('crop-x').value=String((requestedEditor.crop?.x??.5)*100);$('crop-y').value=String((requestedEditor.crop?.y??.5)*100);editorLayout=['stacked','circle'].includes(requestedEditor.layout)?requestedEditor.layout:'full';$('circle-size').value=String((requestedEditor.camera_size??.35)*100);$('circle-x').value=String((requestedEditor.camera_position?.x??.05)*100);$('circle-y').value=String((requestedEditor.camera_position?.y??.05)*100);if(requestedEditor.camera)editorRegions.camera=normalizeRegion(requestedEditor.camera);if(requestedEditor.content)editorRegions.content=normalizeRegion(requestedEditor.content);$('camera-height').value=String((requestedEditor.camera_height??.3)*100);if(requestedEditor.captions?.enabled){pendingCaptionCues=validCaptionCues(requestedEditor.captions.cues);$('captions-enabled').checked=true;}syncRegionFields();renderSections();updateSection();
    }
    if(automatic) {
      $('format').value='video';$('download-scope').value='full';updateSection();
      if(currentItems.length>1)message('Este post tem vários vídeos. Marque quais deseja baixar e clique em Baixar.');
      else if(defaultQuality!=='best'&&$('quality').value!==defaultQuality)message(`Este vídeo não está disponível até ${defaultQuality}p, sua qualidade padrão. Confira as opções e escolha a qualidade antes de baixar.`,true);
      else await startDownloads(true);
    }
  }catch(error){message(error.message,true);}finally{searching=false;$('cookie-file').disabled=false;$('remove-cookies').disabled=false;$('inspect').disabled=false;$('paste-download').disabled=false;$('inspect').innerHTML='Buscar vídeo'+icon('arrow');}
}
function invalidateVideo(){detachLive();resetPreview();resetEditor();current=null;inspection=null;currentItems=[];selectedVideos.clear();$('custom-name').value='';$('video-result').hidden=true;}
function resetPreview() {
  resetCaptionState();
  const previous=previewId;previewId='';previewGeneration++;clearTimeout(previewTimer);previewPlayingSection=false;
  previewFPS=null;previewDimensions=null;previewFrameTimes=null;previewFrameNote='';frameSeekBusy=false;cropDrag=null;$('crop-overlay').setAttribute('hidden','');
  clearVerticalPreview();
  const player=$('preview-player');player.pause();player.removeAttribute('src');player.load();
  $('preview-player-wrap').hidden=true;$('prepare-preview').disabled=false;$('prepare-preview').innerHTML=icon('play')+'Preparar prévia';
  $('cancel-preview').hidden=true;
  $('preview-status').textContent='A prévia é preparada no seu computador e pode levar alguns instantes.';
  $('preview-status').classList.remove('invalid');
  ['mark-start','mark-end','play-section','frame-back','frame-forward'].forEach(id=>$(id).disabled=true);
  if(previous&&token)api('preview/cancel',{id:previous}).catch(()=>{});
  renderCaptionState();
}
function showPreview(data,generation) {
  if(generation!==previewGeneration)return;
  if(data.id)previewId=data.id;
  if(data.status==='cancelled'||data.status==='failed'||data.status==='error'||data.error)throw new Error(data.error||(data.status==='cancelled'?'A prévia foi cancelada.':'Não foi possível preparar a prévia.'));
  if(data.status==='ready'&&data.media_url) {
    if(current?.clip?.id===data.id)current.clip.key=new URL(data.media_url,location.href).searchParams.get('key');
    previewFPS=data.fps;previewDimensions={width:data.width,height:data.height};
    previewFrameTimes=data.frames_indexed===true?validatedFrameTimes(data.frame_times,Number(data.duration)):null;
    previewFrameNote=data.frames_indexed===true&&!previewFrameTimes?'A lista de quadros não pôde ser usada nesta página; os passos são estimados.':typeof data.frame_note==='string'?data.frame_note:'';
    const media=new URL(data.media_url,location.href);
    if(media.origin!==location.origin)throw new Error('O endereço da prévia não é válido. Busque o vídeo novamente.');
    const player=$('preview-player');player.src=media.href;player.load();
    $('preview-player-wrap').hidden=false;$('prepare-preview').disabled=false;$('prepare-preview').innerHTML=icon('play')+'Preparar novamente';
    $('cancel-preview').textContent='Descartar prévia';
    if(current?.clip){$('prepare-preview').disabled=true;$('prepare-preview').textContent='Clipe da live pronto';$('cancel-preview').hidden=true;}
    $('preview-status').textContent='Dê play e use Marcar início e Marcar fim na posição desejada. A prévia pode ter qualidade menor; o download usa a qualidade escolhida.';
    return;
  }
  $('preview-status').textContent=data.message||data.stage||'Preparando uma prévia no seu computador. Vídeos longos podem levar mais tempo.';
  if(!previewId)throw new Error('O Keep não retornou a prévia. Tente novamente.');
  previewTimer=setTimeout(async()=>{
    try {showPreview(await api('preview?id='+encodeURIComponent(previewId)),generation);}
    catch(error){previewError(error,generation);}
  },1600);
}
function previewError(error,generation) {
  if(generation!==previewGeneration)return;
  $('preview-status').textContent=error.message+' Você ainda pode digitar o início e o fim acima.';
  $('preview-status').classList.add('invalid');$('prepare-preview').disabled=false;$('prepare-preview').innerHTML=icon('play')+'Tentar prévia novamente';
}
$('prepare-preview').addEventListener('click',async()=>{
  if(!current||selectedItems().length!==1)return;
  resetPreview();const generation=previewGeneration;
  $('cancel-preview').hidden=false;$('cancel-preview').textContent='Cancelar prévia';
  $('prepare-preview').disabled=true;$('preview-status').textContent='Preparando a prévia… Mantenha o Keep aberto.';
  try {
    const data=await api('preview',videoRequest());
    if(generation!==previewGeneration){if(data.id)api('preview/cancel',{id:data.id}).catch(()=>{});return;}
    showPreview(data,generation);
  } catch(error){previewError(error,generation);}
});
$('cancel-preview').addEventListener('click',()=>{resetPreview();$('preview-status').textContent='Prévia descartada. Os horários escolhidos foram mantidos.';});
$('preview-player').addEventListener('loadedmetadata',()=>{
  if(pendingCaptionCues&&previewId){captionCues=pendingCaptionCues;pendingCaptionCues=null;captionSections=editSections.length?editSections.map(part=>({...part})):null;captionBinding=previewId;captionState='ready';renderCaptionReview();$('captions-status').textContent='Legendas da edição anterior recuperadas. Confira o texto na nova prévia.';}
  ['mark-start','mark-end'].forEach(id=>$(id).disabled=false);updateSection();
  $('preview-clock').textContent=timeLabel($('preview-player').currentTime)+' / '+timeLabel($('preview-player').duration||current?.duration||0);
  updateVertical();updateFrameInfo();updateTimelineClock();
});
$('preview-player').addEventListener('timeupdate',()=>{
  const player=$('preview-player');
  $('preview-clock').textContent=timeLabel(player.currentTime)+' / '+timeLabel(player.duration||current?.duration||0);
  updateTimelineClock();
  if(previewPlayingSection){try{if(player.currentTime>=parseTime($('section-end').value)){player.pause();previewPlayingSection=false;}}catch{previewPlayingSection=false;}}
});
$('preview-player').addEventListener('error',()=>{if($('preview-player').hasAttribute('src'))previewError(new Error('Não foi possível reproduzir esta prévia.'),previewGeneration);});
['start','end'].forEach(edge=>$('mark-'+edge).addEventListener('click',()=>{
  const player=$('preview-player');if(!Number.isFinite(player.currentTime))return;
  const duration=Number.isFinite(current?.duration)?current.duration:player.duration;
  $('section-'+edge).value=timeLabel(Math.min(player.currentTime,duration));previewPlayingSection=false;updateSection();
}));
$('play-section').addEventListener('click',async()=>{
  try {
    const section=selectedSection();if(!section)return;
    const player=$('preview-player');player.currentTime=section.start;previewPlayingSection=true;await player.play();
  } catch(error){previewPlayingSection=false;toast(error.message);}
});
function updateCookiePanel(){$('cookie-import').hidden=!local || $('browser-session').value!=='imported';}
function showCookieStatus(data){
 const platforms=data.platforms||[];
 $('cookie-status').textContent=platforms.length?'Cookies prontos para '+platforms.join(' e ')+'.':'Nenhum arquivo importado.';
 $('remove-cookies').hidden=!platforms.length;
}
$('browser-session').addEventListener('change',()=>{browserSelectionChanged=true;invalidateVideo();updateCookiePanel();message('');});
$('cookie-file').addEventListener('change',async()=>{
 const file=$('cookie-file').files[0];if(!file||!local||!token)return;
 $('cookie-file').disabled=true;$('remove-cookies').disabled=true;$('inspect').disabled=true;
 try{
  if(file.size>1024*1024)throw new Error('Escolha um arquivo cookies.txt de até 1 MB.');
  const result=await api('cookies/import',{content:await file.text()});
  showCookieStatus(result);invalidateVideo();message('Cookies importados. Agora clique em Buscar vídeo.'+(result.ignored?' Cookies vencidos ou de outros sites foram ignorados.':''));
 }catch(error){message(error.message,true);}finally{$('cookie-file').value='';$('cookie-file').disabled=false;$('remove-cookies').disabled=false;$('inspect').disabled=searching;}
});
$('remove-cookies').addEventListener('click',async()=>{
 $('remove-cookies').disabled=true;
 try{showCookieStatus(await api('cookies/remove',{}));invalidateVideo();message('Cookies removidos do Keep. O arquivo original exportado continua onde você o salvou.');}
 catch(error){message(error.message,true);}finally{$('remove-cookies').disabled=false;}
});
$('format').addEventListener('change',()=>{
 if($('format').value==='gif'&&Number(current?.duration)>0&&selectedItems().length===1){$('download-scope').value='section';if(!editSections.length){let start=0;try{start=parseTime($('section-start').value);}catch{}start=Math.min(start,Math.max(0,current.duration-.1));$('section-start').value=timeLabel(start);$('section-end').value=timeLabel(Math.min(current.duration,start+30));}}
 updateSection();
});
$('download-scope').addEventListener('change',updateSection);
['section-start','section-end'].forEach(id=>$(id).addEventListener('input',updateSection));
$('download-button').addEventListener('click',()=>startDownloads());
function buildDownloadRequests(automatic=false) {
 if(current?.clip&&!current.clip.key)throw new Error('Aguarde a prévia do clipe ficar pronta.');
 const selected=selectedItems();if(!selected.length)throw new Error('Marque pelo menos um vídeo do post.');
 const browser=$('browser-session').value,mode=automatic?'video':$('format').value,quality=automatic?defaultQuality:$('quality').value;
 let sections=automatic?null:plannedSections();const customName=automatic?'':$('custom-name').value.trim();
 if(mode==='gif'&&!sections)throw new Error('Escolha um trecho de até 30 segundos para criar um GIF.');
 const base=item=>({...videoRequest(item),browser,title:item.title,mode,quality,section:null,...(customName?{custom_name:customName}:{})});
 const vertical=!automatic&&mode==='video'&&verticalEnabled(),captions=!automatic&&captionsEnabled();
 if(captions)captionExportReady();
 if(!sections&&(vertical||captions)){
  if(selected.length!==1||!Number.isFinite(current?.duration)||current.duration<=0)throw new Error('Selecione um vídeo com duração informada para editar.');
  sections=[{start:0,end:current.duration}];
 }
 if(!sections)return selected.map(base);
 if(selected.length!==1)throw new Error('Para editar trechos, selecione apenas um vídeo.');
 const crop={x:Number($('crop-x').value)/100,y:Number($('crop-y').value)/100};
 const groups=$('download-scope').value==='section'&&$('section-output').value!=='join'?sections.map(section=>[section]):[sections];
 const composition=vertical&&editorLayout!=='full'?{layout:editorLayout,camera:{...editorRegions.camera},content:{...editorRegions.content},...(editorLayout==='circle'?{camera_size:Number($('circle-size').value)/100,camera_position:{x:Number($('circle-x').value)/100,y:Number($('circle-y').value)/100}}:{camera_height:Number($('camera-height').value)/100})}:{layout:'full'};
 return groups.map((group,index)=>({...base(selected[0]),...(customName&&groups.length>1?{custom_name:customName.slice(0,105)+'_trecho_'+String(index+1).padStart(2,'0')}:{ }),edit:{sections:group.map(item=>({...item})),vertical,crop,...composition,...(captions?{captions:{enabled:true,cues:validCaptionCues(captionCues)}}:{})}}));
}
async function startDownloads(automatic = false) {
 if(!current||startingDownload||batchRunning)return;
 let requests;try{requests=buildDownloadRequests(automatic);}catch(error){message(error.message,true);return;}
 startingDownload=true;$('download-button').disabled=true;$('paste-download').disabled=true;
 let added=0;const failures=[];
 try{
   for(let index=0;index<requests.length;index++){
     try{if(await queueDownload(requests[index]))added++;}
     catch(error){failures.push((requests.length>1?'Item '+(index+1)+': ':'')+error.message);}
   }
   await refresh();
   if(added)toast(added>1?`${added} downloads adicionados à fila.`:'Download adicionado à fila.');
   message((added?'Acompanhe os downloads abaixo. Até dois baixam ao mesmo tempo; os próximos começam sozinhos. Mantenha o Keep aberto até terminar. ':'')+failures.join(' '),failures.length>0);
 }finally{startingDownload=false;$('paste-download').disabled=false;updateSection();}
}
function duplicateConfirmation(matches) {
  const previous=matches.slice(0,3).map(item=>'• '+(item.title||item.filename||'Vídeo já baixado')).join('\n');
  return confirm('Este vídeo já foi baixado com estas opções:\n\n'+previous+'\n\nDeseja baixar novamente?');
}
async function queueDownload(request) {
  const duplicates=await api('duplicates',request);
  if(duplicates.matches?.length){if(!duplicateConfirmation(duplicates.matches))return false;request={...request,allow_duplicate:true};}
  try{await api('download',request);return true;}
  catch(error){
    if(error.status===409&&error.data?.code==='duplicate'&&!request.allow_duplicate){
      if(!duplicateConfirmation(error.data.matches||[]))return false;
      await api('download',{...request,allow_duplicate:true});return true;
    }
    throw error;
  }
}
function renderBatch() {
  $('batch-results').innerHTML=batchEntries.map((entry,index)=>`<li class="batch-${esc(entry.state||'waiting')}"><span>${index+1}. ${esc(entry.url)}</span><strong>${esc(entry.message||'Aguardando')}</strong></li>`).join('');
}
function validBatchURL(value) {
  try{const u=new URL(value);return ['http:','https:'].includes(u.protocol)&&!u.username&&!u.password&&['youtube.com','www.youtube.com','m.youtube.com','music.youtube.com','youtu.be','x.com','www.x.com','twitter.com','www.twitter.com','mobile.twitter.com','twitch.tv','www.twitch.tv','m.twitch.tv','go.twitch.tv','clips.twitch.tv','player.twitch.tv'].includes(u.hostname)&&(!u.port||['80','443'].includes(u.port));}catch{return false;}
}
function setBatchBusy(busy) {
  batchRunning=busy;$('batch-start').disabled=busy;$('batch-cancel').hidden=!busy;$('batch-links').disabled=busy;
  ['inspect','paste-download','browser-session','cookie-file','remove-cookies'].forEach(id=>$(id).disabled=busy);
  $('download-button').disabled=busy||!current;
}
$('batch-cancel').addEventListener('click',()=>{batchCancel=true;$('batch-status').textContent='A lista vai parar após a solicitação atual. Os downloads já adicionados continuam na fila.';});
$('batch-start').addEventListener('click',async()=>{
  if(batchRunning||searching||startingDownload)return;
  if(!local||!token){toast('Abra o Keep no computador para adicionar a lista.');return;}
  const raw=$('batch-links').value.trim();
  if(!raw){$('batch-status').textContent='Cole um link por linha.';return;}
  if(raw!==batchSource||batchNextIndex>=batchEntries.length){
    const lines=raw.split(/\r?\n/).map(value=>value.trim()).filter(Boolean);
    if(lines.length>100){$('batch-status').textContent='Adicione até 100 links por lista.';return;}
    batchSource=raw;batchEntries=lines.map(url=>({url,state:'waiting'}));batchNextIndex=0;
  }
  const browser=$('browser-session').value,quality=defaultQuality;
  let added=0,pausedForSelection=false;batchCancel=false;setBatchBusy(true);renderBatch();
  try{
    for(let index=batchNextIndex;index<batchEntries.length;index++){
      if(batchCancel)break;
      const entry=batchEntries[index];entry.state='checking';entry.message='Verificando vídeo…';renderBatch();
      $('batch-status').textContent=`Verificando ${index+1} de ${batchEntries.length}. Os downloads adicionados começam pela fila.`;
      try{
        if(!validBatchURL(entry.url))throw new Error('Link inválido ou plataforma não compatível.');
        const data=await api('inspect',{url:entry.url,browser});
        if(data.live)throw new Error('Para criar clipes de uma live, abra o link individualmente e clique em Acompanhar live.');
        const items=Array.isArray(data.items)&&data.items.length?data.items:[data];
        if(items.length>1){
          inspection=data;currentItems=items.map((item,i)=>({...data,...item,items:undefined,video_index:item.video_index??i+1}));selectedVideos=new Set();
          $('video-url').value=entry.url;showCurrentVideo(currentItems[0]);renderPostVideos();$('video-result').hidden=false;
          entry.state='selection';entry.message='Escolha os vídeos do post no formulário acima.';pausedForSelection=true;
          message('A lista foi pausada neste post do X. Marque os vídeos acima e clique em Baixar. Depois use Continuar lista.');
        }else{
          const item=items[0];
          if(quality!=='best'&&item.heights?.length&&!item.heights.some(height=>height<=Number(quality)))throw new Error(`Nenhuma opção até ${quality}p. Abra este link individualmente para escolher outra qualidade.`);
          const accepted=await queueDownload({url:data.url||entry.url,browser,inspection_id:data.inspection_id,video_index:item.video_index??1,...(item.video_id?{video_id:item.video_id}:{}),title:item.title,mode:'video',quality,section:null});
          entry.state=accepted?'added':'skipped';entry.message=accepted?'Adicionado à fila.':'Duplicado ignorado por você.';if(accepted)added++;
        }
      }catch(error){entry.state='error';entry.message=error.message;}
      batchNextIndex=index+1;renderBatch();
      if(pausedForSelection)break;
    }
    await refresh();refreshStorage(true);
    const remaining=batchEntries.length-batchNextIndex;
    $('batch-status').textContent=(pausedForSelection?'Lista pausada para escolher os vídeos do post. ':batchCancel?'Lista interrompida. ':'Lista verificada. ')+`${added} download${added===1?'':'s'} adicionado${added===1?'':'s'} nesta rodada.`+(remaining?` ${remaining} link${remaining===1?'':'s'} aguardando.`:' Confira os resultados abaixo.');
    $('batch-start').textContent=remaining?'Continuar lista':'Adicionar lista à fila';
  }finally{setBatchBusy(false);updateSection();}
});
function empty(title,subtitle){return `<div class="empty"><div class="empty-art">${icon('download')}</div><h3>${title}</h3><p>${subtitle}</p></div>`;}
function bytesLabel(bytes) {
 const amount=Number(bytes);if(!Number.isFinite(amount)||amount<0)return '';
 const units=['B','KB','MB','GB','TB'];let unit=0,value=amount;
 while(value>=1024&&unit<units.length-1){value/=1024;unit++;}
 return value.toLocaleString('pt-BR',{maximumFractionDigits:unit?1:0})+' '+units[unit];
}
function etaLabel(seconds) {
 const total=Math.ceil(Number(seconds));if(!Number.isFinite(total)||total<0)return '';
 if(total<1)return 'Menos de 1 s restante';
 const hours=Math.floor(total/3600),minutes=Math.floor(total/60)%60,remaining=total%60;
 return (hours?`${hours} h${minutes?' '+minutes+' min':''}`:minutes?`${minutes} min${remaining?' '+remaining+' s':''}`:`${remaining} s`)+' restantes';
}
function jobStage(job) {
 if(job.status==='queued')return job.queue_position?`${job.queue_position}º na fila`:'Na fila';
 if(job.status==='paused')return 'Pausado · pronto para retomar';
 if(job.status==='complete')return job.file_deleted?'Arquivo excluído · mantido no histórico':job.file_missing?'Arquivo não encontrado no computador':'Concluído';
 if(job.status==='failed')return 'Não foi possível baixar';
 if(job.status==='cancelled')return 'Cancelado';
 if(job.phase==='processing')return job.stage&&job.stage!=='Downloading'?job.stage:'Preparando arquivo';
 return {'Downloading':'Baixando','Finishing file':'Finalizando arquivo','Ready':'Pronto'}[job.stage]||job.stage||'Baixando';
}
function jobMarkup(job){
 const active=job.status==='downloading',processing=active&&job.phase==='processing',busy=pendingJobActions.has(job.id)||active&&/paus|cancel|interromp|stopp/i.test(job.stage||'');
 const disabled=busy?' disabled':'';
 const percent=Math.min(100,Math.max(0,Number(job.progress)||0));
 const quality=job.mode==='gif'?'GIF':job.mode==='audio'?'MP3':`MP4 · ${job.quality==='best'?'Melhor disponível':'Até '+job.quality+'p'}`;
 const missing=job.status==='complete'&&(job.file_missing===true||!!job.file_deleted);
 const fileBytes=job.file_bytes??job.bytes;
 const size=!missing&&fileBytes?' · '+bytesLabel(fileBytes):'';
 const section=job.edit?.sections?.length>1?` · Montagem de ${job.edit.sections.length} trechos`:job.section?` · Trecho ${timeLabel(job.section.start)}–${timeLabel(job.section.end)}`:'';
 const effectiveDuration=Number(job.effective_edit?.duration),editDetails=job.status==='complete'&&Number.isFinite(effectiveDuration)&&effectiveDuration>0?`<p class="transfer-details">Duração final: ${esc(timeLabel(effectiveDuration))}</p>`:'';
 const transfers=[];
 if(active&&!processing){
  if(Number(job.downloaded_bytes)>0)transfers.push(bytesLabel(job.downloaded_bytes)+(Number(job.total_bytes)>0?' de '+bytesLabel(job.total_bytes):''));
  if(Number(job.speed)>0)transfers.push(bytesLabel(job.speed)+'/s');
  if(job.eta!==null&&job.eta!==undefined&&etaLabel(job.eta))transfers.push(etaLabel(job.eta));
 }
 if(processing)transfers.push('Juntando ou convertendo o arquivo. Essa etapa pode levar alguns minutos.');
 if(job.status==='queued')transfers.push('Começa automaticamente quando houver uma vaga.');
 if(job.status==='paused')transfers.push(Number(job.partial_bytes)>0?bytesLabel(job.partial_bytes)+' guardados para retomar.':'Aguardando você retomar.');
 if(active&&job.retry_at)transfers.push('Nova tentativa automática em instantes.');
 const queue=queuedJobs(),queueIndex=queue.findIndex(item=>item.id===job.id);
 const queueControls=job.status==='queued'?`<div class="queue-controls"><button class="queue-handle" data-queue-drag="${esc(job.id)}" draggable="true" title="Arraste para mudar a ordem da fila" aria-label="Arrastar ${esc(job.title)} na fila"${queueBusy?' disabled':''}>⠿</button><button data-queue-move="up" data-id="${esc(job.id)}" aria-label="Subir ${esc(job.title)} na fila"${queueBusy||queueIndex<1?' disabled':''}>↑</button><button data-queue-move="down" data-id="${esc(job.id)}" aria-label="Descer ${esc(job.title)} na fila"${queueBusy||queueIndex===queue.length-1?' disabled':''}>↓</button></div>`:'';
 let controls='';
 if(job.status==='complete')controls=missing?'':`<a href="/download/${esc(job.id)}" download>Salvar arquivo ↓</a><button class="delete-file" data-action="delete-file" data-id="${esc(job.id)}"${disabled}>Excluir arquivo</button>`;
 else if(active)controls=`<button data-action="pause" data-id="${esc(job.id)}"${disabled}>Pausar</button><button data-action="cancel" data-id="${esc(job.id)}"${disabled}>Cancelar</button>`;
 else if(job.status==='queued')controls=`<button data-action="cancel" data-id="${esc(job.id)}"${disabled}>Tirar da fila</button>`;
 else if(job.status==='paused')controls=`<button class="resume-job" data-action="resume" data-id="${esc(job.id)}"${disabled}>Retomar</button><button data-action="cancel" data-id="${esc(job.id)}"${disabled}>Descartar</button>`;
 else controls=`<button data-retry="${esc(job.id)}"${disabled}>Tentar novamente ↗</button>`;
 if(job.cleanup_error&&!active&&job.status!=='queued')controls+=`<button data-action="cleanup" data-id="${esc(job.id)}"${disabled}>Limpar parciais</button>`;
 const nextAction=job.next_action||job.diagnostic_action;
 return `<article class="job job-${esc(job.status)}" data-job-id="${esc(job.id)}"><div class="job-icon">${icon(icons[job.platform]||'play')}</div><div class="job-main"><h3 title="${esc(job.title)}">${esc(job.title)}</h3><p>${esc(job.platform)} · ${esc(quality)}${esc(section)}${esc(size)}</p><p class="job-status">${esc(jobStage(job))}${active&&!processing?' · '+Math.round(percent)+'%':''}</p>${active?`<progress class="progress${processing?' processing':''}"${processing?'':` value="${percent}"`} max="100" aria-label="${processing?'Preparando arquivo':'Progresso do download'}"></progress>`:''}${transfers.length?`<p class="transfer-details">${esc(transfers.join(' · '))}</p>`:''}${job.section_note?`<p class="transfer-details">${esc(job.section_note)}</p>`:''}${job.edit_note?`<p class="transfer-details">${esc(job.edit_note)}</p>`:''}${editDetails}${job.error?`<p class="job-error">${esc(job.error)}</p>`:''}${nextAction?`<p class="diagnostic-action">${esc(nextAction)}</p>`:''}${job.cleanup_error?`<p class="job-error">${esc(job.cleanup_error)}</p>`:''}${jobActionErrors.has(job.id)?`<p class="job-error" role="status">${esc(jobActionErrors.get(job.id))}</p>`:''}</div><div class="job-actions">${queueControls}<div class="job-controls">${controls}</div><div class="job-locations">${job.status==='failed'?`<button data-action="diagnostic" data-id="${esc(job.id)}"${disabled}>Copiar relatório de erro</button>`:''}<button data-action="copy-link" data-id="${esc(job.id)}"${disabled}>Copiar link original</button><button data-action="open-folder" data-id="${esc(job.id)}"${disabled}>${icon('folder')}Abrir pasta</button>${missing?'':`<button data-action="show-file" data-id="${esc(job.id)}"${disabled}>${session?.platform==='windows'?'Mostrar no Explorer':'Mostrar na pasta'}</button>`}</div></div></article>`;
}
function queuedJobs(){return jobs.filter(job=>job.status==='queued').sort((a,b)=>(a.queue_position??a.queue_order??a.created??0)-(b.queue_position??b.queue_order??b.created??0));}
function renderJobs(){
 $('count').textContent=jobs.length;$('recent-count').textContent=jobs.length;
 $('recent-list').innerHTML=jobs.length?jobs.slice(0,3).map(jobMarkup).join(''):empty('Seu primeiro download começa com um link','Os vídeos baixados aparecerão aqui. Vamos encontrar algo legal?');
 const term=$('search').value.toLowerCase();const filter=$('status-filter').value;
 const ordered=[...jobs.filter(job=>job.status==='downloading'),...queuedJobs(),...jobs.filter(job=>!['downloading','queued'].includes(job.status))];
 const visible=ordered.filter(job=>(filter==='all'||job.status===filter)&&`${job.title} ${job.platform}`.toLowerCase().includes(term));
 $('library-list').innerHTML=visible.length?visible.map(jobMarkup).join(''):empty(jobs.length?'Nenhum resultado encontrado':'Um espaço para seus favoritos',jobs.length?'Tente outra busca ou outro filtro.':'Busque um vídeo para começar sua coleção.');
 const active=jobs.filter(job=>job.status==='downloading').length,queued=jobs.filter(job=>job.status==='queued').length,paused=jobs.filter(job=>job.status==='paused').length;
 $('queue-summary').textContent=[active?`${active} em andamento`:null,queued?`${queued} na fila`:null,paused?`${paused} pausado${paused===1?'':'s'}`:null].filter(Boolean).join(' · ')||'Até dois downloads ao mesmo tempo. Os próximos começam automaticamente.';
}
async function refresh(){if(!token||keepClosed)return;try{const data=await api('jobs');if(keepClosed)return;jobs=data;if(!queueDragId)renderJobs();refreshStorage();$('connection').className='connection online';$('connection').innerHTML='<span></span>Keep '+esc(session?.version||'2.4.0')+' conectado';}catch{if(keepClosed)return;$('connection').className='connection';$('connection').innerHTML='<span></span>'+(updateStatus?.state==='applying'?'Keep reiniciando…':'Desconectado · reinicie o Keep');}}
async function refreshStorage(force=false) {
 if(!local||!token||keepClosed||(!force&&Date.now()-storageCheckedAt<12000))return;
 storageCheckedAt=Date.now();
 try{const data=await api('storage'),text=bytesLabel(data.total_bytes||0)+' em arquivos registrados no Keep · '+(data.file_count||0)+' arquivo'+(data.file_count===1?'':'s');$('storage-library').textContent=text;$('storage-settings').textContent=text;}
 catch{if(force){$('storage-library').textContent='Não foi possível consultar o espaço ocupado.';$('storage-settings').textContent='Não foi possível consultar o espaço ocupado.';}}
}
async function reorderQueue(id,targetId,direction=null) {
 if(queueBusy||!token)return;
 const ids=queuedJobs().map(job=>job.id),from=ids.indexOf(id);
 if(from<0)return;
 const target=direction==='up'?from-1:direction==='down'?from+1:ids.indexOf(targetId);
 if(target<0||target>=ids.length||target===from)return;
 ids.splice(from,1);ids.splice(target,0,id);queueBusy=true;renderJobs();
 try{await api('queue/reorder',{ids});toast('Ordem da fila atualizada.');await refresh();}
 catch(error){toast(error.message);await refresh();}
 finally{queueBusy=false;renderJobs();}
}
document.addEventListener('dragstart',event=>{
 const handle=event.target.closest('[data-queue-drag]');if(!handle||queueBusy||!token)return;
 queueDragId=handle.dataset.queueDrag;event.dataTransfer.effectAllowed='move';event.dataTransfer.setData('text/plain',queueDragId);handle.closest('.job')?.classList.add('dragging');
});
document.addEventListener('dragover',event=>{const row=event.target.closest('.job-queued');if(queueDragId&&row){event.preventDefault();event.dataTransfer.dropEffect='move';document.querySelectorAll('.queue-drop').forEach(item=>item.classList.remove('queue-drop'));row.classList.add('queue-drop');}});
document.addEventListener('drop',event=>{const row=event.target.closest('.job-queued');if(!queueDragId||!row)return;event.preventDefault();const id=queueDragId;queueDragId='';document.querySelectorAll('.queue-drop').forEach(item=>item.classList.remove('queue-drop'));reorderQueue(id,row.dataset.jobId);});
document.addEventListener('dragend',()=>{queueDragId='';renderJobs();});
async function copyText(text) {
 try {if(navigator.clipboard?.writeText){await navigator.clipboard.writeText(text);return;}}catch{}
 const previous=document.activeElement,field=document.createElement('textarea');
 field.value=text;field.className='clipboard-copy';field.readOnly=true;field.setAttribute('aria-label','Texto para copiar');document.body.append(field);
 try{field.select();if(!document.execCommand('copy'))throw new Error('Não foi possível copiar. Confira a permissão da área de transferência no navegador.');}
 finally{field.remove();previous?.focus({preventScroll:true});}
}
document.addEventListener('click',async e=>{
 const move=e.target.closest('[data-queue-move]');if(move){reorderQueue(move.dataset.id,null,move.dataset.queueMove);return;}
 const action=e.target.closest('[data-action]'),retry=e.target.closest('[data-retry]');
 if(action){
  const id=action.dataset.id,operation=action.dataset.action,job=jobs.find(item=>item.id===id);if(!job||pendingJobActions.has(id))return;
  if(operation==='copy-link'){try{await copyText(job.url);toast('Link original copiado.');}catch(error){toast(error.message);}return;}
  if(operation==='diagnostic'){try{const report=await api('diagnostic',{id});await copyText(report.text);toast('Relatório copiado, sem cookies nem dados de sessão.');}catch(error){toast(error.message);}return;}
  if(operation==='cancel'&&job.status==='paused'&&!confirm('Descartar este download pausado? Os arquivos parciais serão apagados.'))return;
  if(operation==='delete-file'&&!confirm('Apagar permanentemente o arquivo “'+(job.filename||job.title)+'” deste computador?\n\nO registro continuará no histórico do Keep. Esta ação não pode ser desfeita.'))return;
  pendingJobActions.add(id);jobActionErrors.delete(id);renderJobs();
  try{const actionResult=await api(operation,{id,...(operation==='resume'?{browser:browserSelectionChanged?$('browser-session').value:job.browser||'none'}:{}),...(operation==='delete-file'?{confirm:true}:{})});if(operation==='delete-file')toast('Arquivo excluído do computador. O histórico foi mantido.');if(operation==='resume')toast('Download adicionado à fila para retomar.');if(operation==='pause')toast('Pausando e guardando o progresso…');if(operation==='cancel')toast('Cancelando e removendo os arquivos parciais…');if(operation==='cleanup')toast(actionResult.ok?'Arquivos parciais removidos.':'Alguns parciais continuam em uso. Confira o aviso no download.');await refresh();}
  catch(error){jobActionErrors.set(id,error.message);toast(error.message);}
  finally{pendingJobActions.delete(id);renderJobs();}
 }
 if(retry){const job=jobs.find(j=>j.id===retry.dataset.retry);if(job){view('new');$('video-url').value=job.url;$('browser-session').value=job.browser||'none';retrySection=job.section||null;retryEditor=job.edit?{...job.edit,mode:job.mode}:null;retryDownloadOptions={mode:job.mode,quality:job.quality,custom_name:job.custom_name};updateCookiePanel();$('link-form').requestSubmit();}}
});
$('clear-history').addEventListener('click',async()=>{if(!local||!jobs.length){toast('Seu histórico já está vazio.');return;}if(!confirm('Limpar os downloads concluídos, com erro e cancelados do histórico? Seus arquivos, a fila e os downloads pausados serão mantidos.'))return;try{await api('clear',{});await refresh();toast('Histórico limpo. Seus arquivos, a fila e os pausados continuam salvos.');}catch(error){toast(error.message);}});
$('search').addEventListener('input',renderJobs);$('status-filter').addEventListener('change',renderJobs);
function applyPreferences(data, initial = false) {
  preferences={...preferences,...data};
  defaultQuality=preferences.default_quality;
  $('default-quality').value=defaultQuality;
  $('default-browser').value=preferences.default_browser;
  $('enable-paste-download').checked=preferences.paste_and_download===true;
  $('platform-folders').checked=preferences.platform_folders===true;
  $('notifications-enabled').checked=preferences.notifications===true;
  $('paste-download').hidden=!preferences.paste_and_download;
  if(initial || !browserSelectionChanged){$('browser-session').value=preferences.default_browser;updateCookiePanel();}
  $('default-cookie-note').textContent=preferences.default_browser==='imported'?'A opção fica salva. Por privacidade, importe o arquivo novamente sempre que reabrir o Keep.':'Apenas a opção escolhida fica salva. O Keep lê os cookies desse navegador quando você usa o download.';
  try{localStorage.setItem('keep-quality',defaultQuality);}catch{}
}
async function savePreferences() {
  if(preferencesSaving)return;
  const data={paste_and_download:$('enable-paste-download').checked,default_browser:$('default-browser').value,default_quality:$('default-quality').value,platform_folders:$('platform-folders').checked,notifications:$('notifications-enabled').checked};
  if(!local||!token){$('preferences-status').textContent='Abra o Keep no seu computador para salvar estas preferências.';applyPreferences(preferences);return;}
  preferencesSaving=true;['enable-paste-download','default-browser','default-quality','platform-folders','notifications-enabled'].forEach(id=>$(id).disabled=true);
  $('preferences-status').textContent='Salvando…';$('preferences-status').classList.remove('invalid');
  try {
    const result=await api('settings/preferences',data);applyPreferences(result.preferences||result);
    $('preferences-status').textContent='Preferências salvas neste computador.';toast('Preferências salvas.');
  } catch(error) {applyPreferences(preferences);$('preferences-status').textContent=error.message;$('preferences-status').classList.add('invalid');}
  finally {preferencesSaving=false;['enable-paste-download','default-browser','default-quality','platform-folders','notifications-enabled'].forEach(id=>$(id).disabled=false);}
}
['default-quality','default-browser','enable-paste-download','platform-folders','notifications-enabled'].forEach(id=>$(id).addEventListener('change',savePreferences));
function downloadText(filename,text) {
 const url=URL.createObjectURL(new Blob([text],{type:'text/plain;charset=utf-8'})),link=document.createElement('a');
 link.href=url;link.download=filename;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
$('export-preferences').addEventListener('click',async()=>{
 if(!local||!token)return;$('export-preferences').disabled=true;
 try{const backup=await api('settings/export',{});downloadText('Keep-preferencias.txt',JSON.stringify(backup,null,2));$('backup-status').textContent='Backup exportado. Ele contém suas opções, sem cookies nem caminho de pasta.';}
 catch(error){$('backup-status').textContent=error.message;}finally{$('export-preferences').disabled=false;}
});
$('import-preferences').addEventListener('change',async()=>{
 const file=$('import-preferences').files[0];if(!file||!local||!token)return;
 $('import-preferences').disabled=true;
 try{if(file.size>65536)throw new Error('Escolha um backup de preferências de até 64 KB.');let backup;try{backup=JSON.parse(await file.text());}catch{throw new Error('Este arquivo não é um backup válido das preferências do Keep.');}const data=await api('settings/import',{backup});applyPreferences(data.preferences||data);$('backup-status').textContent='Preferências importadas. A pasta atual e os arquivos foram mantidos.';toast('Preferências importadas.');}
 catch(error){$('backup-status').textContent=error.message;}finally{$('import-preferences').disabled=false;$('import-preferences').value='';}
});
let releaseNotes=null;
async function showReleaseNotes(automatic=false) {
 if(!local||!token)return;
 try{
   const data=releaseNotes||await api('release-notes');releaseNotes=data;
   $('release-title').textContent='Novidades do Keep '+(data.version||session?.version||'');
   $('release-list').replaceChildren(...(data.notes||[]).map(note=>{const item=document.createElement('li');item.textContent=note;return item;}));
   let seen;try{seen=localStorage.getItem('keep-seen-version');}catch{}
   if(!automatic||seen!==data.version&&(data.show===true||!!seen))$('release-dialog').showModal();
   try{localStorage.setItem('keep-seen-version',data.version||session?.version||'');}catch{}
 }catch(error){if(!automatic)toast(error.message);}
}
$('show-release-notes').addEventListener('click',()=>showReleaseNotes());
$('close-release-notes').addEventListener('click',()=>$('release-dialog').close());
let updateTransferBusy=false;
function renderDesktop(data) {
 desktopStatus=data;
 const supported=data?.supported===true,connected=local&&!!token&&!keepClosed;
 $('desktop-status').textContent=data?.message||(data?.launcher_installed?'Inicializador criado neste computador.':'Crie o inicializador para encontrar o Keep no menu de aplicativos.');
 $('desktop-status').classList.remove('invalid');
 $('install-launcher').textContent=data?.launcher_installed?'Recriar inicializador':'Criar inicializador com ícone';
 $('install-launcher').disabled=!connected||!supported||desktopWorking;
 $('protocol-enabled').disabled=!connected||!supported||desktopWorking;
 $('protocol-enabled').checked=data?.protocol_enabled===true;
 $('protocol-status').textContent=data?.protocol_enabled?'Abertura pelo site permitida. No site público, clique em Abrir Keep. O navegador pode pedir confirmação.':'Abertura pelo site desativada neste computador. Você pode continuar usando o inicializador.';
 $('configure-hosted-launch').hidden=!data?.protocol_enabled;
 $('launcher-help').textContent=(data?.platform||session?.platform)==='windows'
   ?'Procure Keep no Iniciar. Clique com o botão direito e escolha Fixar em Iniciar; Mais → Fixar na barra de tarefas pode estar disponível. Você também pode abrir Keep.exe na pasta do app.'
   :'Procure Keep no menu de aplicativos do Linux. Use Adicionar aos favoritos ou Fixar, conforme o seu ambiente. Se não aparecer de imediato, reabra o menu ou encerre e inicie sua sessão.';
}
async function refreshDesktop() {
 if(!local||!token||keepClosed)return;
 try{renderDesktop(await api('desktop'));}
 catch(error){$('desktop-status').textContent=error.message;$('desktop-status').classList.add('invalid');$('install-launcher').disabled=true;$('protocol-enabled').disabled=true;}
}
$('install-launcher').addEventListener('click',async()=>{
 if(!local||!token||desktopWorking)return;
 desktopWorking=true;renderDesktop(desktopStatus);$('desktop-status').textContent='Criando o inicializador para este usuário…';
 try{renderDesktop(await api('desktop/launcher',{}));toast('Inicializador do Keep criado. Confira o menu de aplicativos.');}
 catch(error){$('desktop-status').textContent=error.message;$('desktop-status').classList.add('invalid');}
 finally{desktopWorking=false;const supported=desktopStatus?.supported===true&&local&&!!token&&!keepClosed;$('install-launcher').disabled=!supported;$('protocol-enabled').disabled=!supported;}
});
$('protocol-enabled').addEventListener('change',async()=>{
 if(!local||!token||desktopWorking)return;
 const enabled=$('protocol-enabled').checked;desktopWorking=true;renderDesktop(desktopStatus);$('protocol-status').textContent=enabled?'Permitindo a abertura pelo site…':'Desativando a abertura pelo site…';
 try{renderDesktop(await api('desktop/protocol',{enabled}));toast(enabled?'Abertura pelo site permitida. Use o botão Abrir Keep no site público.':'Abertura pelo site desativada.');}
 catch(error){$('protocol-enabled').checked=desktopStatus?.protocol_enabled===true;$('protocol-status').textContent=error.message;}
 finally{desktopWorking=false;const supported=desktopStatus?.supported===true&&local&&!!token&&!keepClosed;$('install-launcher').disabled=!supported;$('protocol-enabled').disabled=!supported;}
});
function updateRestoreNote() {
 const point=restorePoints.find(item=>item.id===$('restore-version').value);
 $('restore-point-note').textContent=point?.integrity==='legacy'?'Esta cópia foi criada por uma versão antiga e não tem a verificação original. Só prossiga se você confia na pasta desta instalação.':point?'A integridade desta cópia será conferida novamente antes de restaurar.':'';
}
async function refreshRestorePoints() {
 if(!local||!token||keepClosed||restoringPoint)return;
 $('refresh-restore-points').disabled=true;
 try{
   const data=await api('update/restore-points');
   restorePoints=Array.isArray(data.points)?data.points.filter(point=>typeof point.id==='string'&&typeof point.version==='string'):[];
   const previous=$('restore-version').value;
   const options=restorePoints.map(point=>{const option=document.createElement('option');option.value=point.id;const date=Number(point.created);option.textContent='Keep '+point.version+(Number.isFinite(date)&&date>0?' — '+new Date(date*1000).toLocaleString('pt-BR'):'');return option;});
   if(!options.length){const option=document.createElement('option');option.value='';option.textContent='Nenhuma cópia compatível';options.push(option);}
   $('restore-version').replaceChildren(...options);
   $('restore-version').value=restorePoints.some(point=>point.id===previous)?previous:restorePoints[0]?.id||'';
   $('restore-status').textContent=data.message||'Escolha uma versão para conferir a restauração.';
   $('restore-status').classList.remove('invalid');updateRestoreNote();
 }catch(error){$('restore-status').textContent=error.message;$('restore-status').classList.add('invalid');}
 finally{renderUpdate(updateStatus||{state:'idle',current_version:session?.version});}
}
$('refresh-restore-points').addEventListener('click',refreshRestorePoints);
$('restore-version').addEventListener('change',updateRestoreNote);
$('prepare-restore').addEventListener('click',async()=>{
 if(!local||!token||restoringPoint)return;
 const point=restorePoints.find(item=>item.id===$('restore-version').value);if(!point)return;
 if(!confirm('Preparar o retorno ao Keep '+point.version+'?\n\nSeus downloads, histórico e preferências atuais serão mantidos. Funções de versões mais novas podem ficar indisponíveis. Depois da conferência, clique em Restaurar e reiniciar para concluir.'))return;
 restoringPoint=true;renderUpdate(updateStatus||{state:'idle'});$('restore-status').textContent='Conferindo e preparando a cópia anterior…';
 try{
   renderUpdate(await api('update/rollback',{id:point.id,confirm:true}));
   $('restore-status').textContent='Cópia preparada. Clique em Restaurar e reiniciar acima para concluir.';$('restore-status').classList.remove('invalid');
   $('apply-update').scrollIntoView?.({block:'center',behavior:'smooth'});$('apply-update').focus({preventScroll:true});toast('Restauração preparada. Seus arquivos ainda não foram alterados.');
 }catch(error){$('restore-status').textContent=error.message;$('restore-status').classList.add('invalid');}
 finally{restoringPoint=false;renderUpdate(updateStatus||{state:'idle'});}
});
function requestHostedLaunch() {
 if(local)return;
 $('hosted-launch-status').textContent='Pedido enviado ao navegador. Confirme Abrir Keep se solicitado. Se o app não abrir, confira a opção Permitir abertura pelo site nas Preferências do Keep instalado.';
 try{location.assign('keep://launch');}
 catch{$('hosted-launch-status').textContent='O navegador não abriu o aplicativo. Clique novamente em Abrir Keep neste computador ou use o inicializador do app.';}
}
function initHostedLaunch() {
 if(local)return;
 $('hosted-launch-panel').hidden=false;
 $('hosted-launch-status').textContent='Clique em Abrir Keep neste computador e confirme no navegador. O app precisa estar instalado e com a abertura pelo site habilitada.';
}
$('hosted-open-keep').addEventListener('click',event=>{event.preventDefault();requestHostedLaunch();});
function renderUpdate(data) {
  updateStatus=data;
  const state=data.state||'idle',busy=['checking','downloading','applying'].includes(state)||updateTransferBusy;
  const rollback=data.operation==='rollback';
  const available=local&&!rollback&&data.update_available===true;
  $('update-banner').hidden=!available;
  $('update-badge').hidden=!available;
  $('update-banner-text').textContent=available?'Keep '+data.latest_version+' disponível. Você está usando a versão '+(data.current_version||session?.version)+'.':'';
  $('update-banner-action').disabled=keepClosed;
  $('download-update').hidden=!data.can_download||rollback;
  $('download-update').disabled=busy||!local||!token;
  $('download-update').textContent=state==='failed'?'Tentar baixar novamente':'Baixar atualização';
  $('update-version').textContent='Versão instalada: '+(data.current_version||session?.version||'2.4.0')+(data.latest_version?(rollback?' · Restaurar: ':' · Mais recente: ')+data.latest_version:'');
  const labels={idle:'Verifique se há uma nova versão disponível.',checking:'Verificando a versão na página de atualizações…',available:'Uma nova versão está disponível.',current:'Você está usando a versão mais recente.',downloading:'Recebendo e verificando a atualização…',ready:'Atualização verificada. Clique em Atualizar e reiniciar para instalar.',applying:'Atualizando e reiniciando o Keep. Aguarde nesta página…',failed:'Não foi possível verificar a atualização.'};
  $('update-status').textContent=data.error||(rollback&&state==='applying'?'Restaurando a versão anterior e reiniciando o Keep. Aguarde nesta página…':data.message)||(rollback&&state==='ready'?'Cópia anterior conferida. Clique em Restaurar e reiniciar para concluir.':labels[state]||labels.idle);
  $('update-status').classList.toggle('invalid',state==='failed'||!!data.error);
  $('check-update').disabled=busy||!local||!token;
  $('update-file').disabled=busy||!local||!token;
  $('apply-update').hidden=state!=='ready'&&state!=='applying';
  $('apply-update').disabled=!data.can_apply||busy;
  $('apply-update').textContent=rollback?'Restaurar e reiniciar':'Atualizar e reiniciar';
  $('refresh-restore-points').disabled=busy||restoringPoint||!local||!token;
  $('restore-version').disabled=busy||restoringPoint||!restorePoints.length||!local||!token;
  $('prepare-restore').disabled=busy||restoringPoint||!restorePoints.length||!local||!token;
}
async function refreshUpdate() {
  if(!local||!token||keepClosed)return;
  try {
    if(updateStatus?.state==='applying') {
      const response=await fetch('/api/session',{cache:'no-store'});
      if(!response.ok)throw new Error('Keep reiniciando');
      const connected=await response.json();
      if(keepClosed)return;
      if(connected.version&&connected.version!==session?.version){location.reload();return;}
      if(connected.token&&connected.token!==token){token=connected.token;session={...session,...connected};}
    }
    const data=await api('update');
    if(updateStatus?.state==='applying'&&data.current_version&&data.current_version!==session?.version){location.reload();return;}
    if(keepClosed)return;renderUpdate(data);
  } catch {
    if(updateStatus?.state==='applying')$('update-status').textContent='O Keep está reiniciando. A conexão será retomada automaticamente.';
  }
  if(keepClosed)return;clearTimeout(updateTimer);updateTimer=setTimeout(refreshUpdate,['checking','downloading','applying'].includes(updateStatus?.state)?1600:6000);
}
function updateError(text) {
  updateTransferBusy=false;renderUpdate({...updateStatus,state:'failed',error:text,can_apply:false});
}
let lastUpdateCheckAt=0;
async function checkForUpdates(automatic=false) {
  if(!local||!token||keepClosed||updateTransferBusy||['checking','downloading','applying','ready'].includes(updateStatus?.state))return;
  lastUpdateCheckAt=Date.now();updateTransferBusy=true;
  renderUpdate({...updateStatus,state:'checking',error:'',message:'Consultando a versão pública mais recente…'});
  try {
    const data=await api('update/check',{});
    updateTransferBusy=false;renderUpdate(data);refreshUpdate();
  } catch(error) {
    updateError(automatic?'Não foi possível verificar atualizações ao abrir. Tente novamente nas Preferências.':error.message);
  }
}
setInterval(()=>{if(document.visibilityState==='visible')checkForUpdates(true);},5*60*1000);
document.addEventListener('visibilitychange',()=>{
 if(document.visibilityState==='visible'&&Date.now()-lastUpdateCheckAt>=60000)checkForUpdates(true);
});
$('check-update').addEventListener('click',()=>checkForUpdates());
$('update-banner-action').addEventListener('click',()=>{
  view('settings');$('check-update').scrollIntoView({block:'center',behavior:'smooth'});$('check-update').focus({preventScroll:true});
});
$('download-update').addEventListener('click',async()=>{
  if(!local||!token||keepClosed||!updateStatus?.can_download||updateTransferBusy)return;
  updateTransferBusy=true;renderUpdate({...updateStatus,state:'downloading',error:'',message:''});
  try{const data=await api('update/download',{});updateTransferBusy=false;renderUpdate(data);refreshUpdate();}
  catch(error){updateError(error.message);}
});
function packageBase64(buffer) {
  const bytes=new Uint8Array(buffer);let binary='';
  for(let index=0;index<bytes.length;index+=32768)binary+=String.fromCharCode(...bytes.subarray(index,index+32768));
  return btoa(binary);
}
$('update-file').addEventListener('change',async()=>{
  const file=$('update-file').files[0];if(!file||!local||!token)return;
  try {
    if(file.size>4*1024*1024)throw new Error('Escolha o pacote compacto de atualização, com até 4 MB.');
    updateTransferBusy=true;renderUpdate({...updateStatus,state:'downloading',error:'',message:'Conferindo o pacote de atualização…'});
    const result=await api('update/receive',{filename:file.name,package_base64:packageBase64(await file.arrayBuffer())});
    updateTransferBusy=false;renderUpdate(result);
  } catch(error){updateError(error.message);}
  finally {$('update-file').value='';}
});
$('apply-update').addEventListener('click',async()=>{
  if(!updateStatus?.can_apply)return;
  try {renderUpdate({...updateStatus,state:'applying',message:'',error:''});renderUpdate(await api('update/apply',{}));refreshUpdate();}
  catch(error){updateError(error.message);}
});
$('quit-keep').addEventListener('click',async()=>{
  if(!local||!token||keepClosed)return;
  if(!confirm('Encerrar o Keep? Downloads ativos serão pausados. O acompanhamento de lives será encerrado e os segundos ainda não salvos serão descartados.'))return;
  $('quit-keep').disabled=true;
  try {
    await api('quit',{});keepClosed=true;token='';
    clearInterval(refreshTimer);clearTimeout(updateTimer);clearTimeout(previewTimer);
    $('preview-player').pause();$('connection').className='connection';$('connection').innerHTML='<span></span>Keep encerrado';
    $('quit-status').textContent='Keep encerrado. Você pode fechar esta aba. Para abrir novamente, use o iniciador do app.';
    $('update-status').textContent='Keep encerrado. Abra o app novamente para verificar atualizações.';
    ['check-update','download-update','apply-update','update-file','inspect','paste-download','download-button','prepare-preview','install-launcher','protocol-enabled','notifications-enabled','refresh-restore-points','restore-version','prepare-restore'].forEach(id=>$(id).disabled=true);
    message('Keep encerrado. Seus downloads foram preservados. Você pode fechar esta aba.');
  } catch(error){$('quit-status').textContent=error.message;$('quit-keep').disabled=false;}
});
function showFolder(data) {
  $('download-folder').textContent=data.folder;
  $('download-folder-path').value=data.folder;
  if(session) Object.assign(session,data);
}
function folderMessage(text,error=false) {
  $('folder-status').textContent=text;
  $('folder-status').className='folder-status'+(error?' invalid':'');
}
function folderBusy(busy) {
  ['choose-folder','save-folder','reset-folder','download-folder-path'].forEach(id=>$(id).disabled=busy);
}
async function saveFolder(folder) {
  if(!local||!token)return;
  folderBusy(true);folderMessage('Conferindo a pasta…');
  try {showFolder(await api('settings/folder',{folder}));folderMessage('Pasta salva. Os próximos downloads serão guardados aqui.');toast('Pasta dos downloads salva.');}
  catch(error){folderMessage(error.message,true);}finally{folderBusy(false);}
}
$('save-folder').addEventListener('click',()=>saveFolder($('download-folder-path').value));
$('reset-folder').addEventListener('click',()=>saveFolder(''));
$('download-folder-path').addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();saveFolder($('download-folder-path').value);}});
$('choose-folder').addEventListener('click',async()=>{
  if(!local||!token)return;
  folderBusy(true);folderMessage('Escolha uma pasta na janela de seleção. Depois clique em Salvar pasta.');
  try {const data=await api('settings/choose-folder',{});if(data.folder){$('download-folder-path').value=data.folder;folderMessage('Pasta selecionada. Clique em Salvar pasta para confirmar.');}else folderMessage('Seleção cancelada. A pasta atual continua em uso.');}
  catch(error){folderMessage(error.message,true);}finally{folderBusy(false);}
});
async function init(){
 const hostedAction=location.hash.slice(1);view(hostedAction||'new');renderJobs();
 if(!local){$('browser-session').disabled=true;folderBusy(true);['enable-paste-download','default-browser','default-quality','check-update','apply-update','update-file','quit-keep','platform-folders','batch-start','export-preferences','import-preferences','show-release-notes','notifications-enabled','install-launcher','protocol-enabled','refresh-restore-points','restore-version','prepare-restore'].forEach(id=>$(id).disabled=true);$('hosted-banner').hidden=false;$('connection').innerHTML='<span></span>App para Windows e Linux';folderMessage('Abra o Keep no seu computador para escolher a pasta.');$('update-status').textContent='Abra o Keep no seu computador para verificar e aplicar atualizações.';initHostedLaunch(hostedAction);return;}
 try{const res=await fetch('/api/session');session=await res.json();token=session.token;showFolder(session);$('platform-label').textContent=session.platform==='linux'?'KEEP PARA LINUX':'KEEP PARA WINDOWS';$('engine-launcher').textContent=session.platform==='linux'?'Atualizar mecanismo.sh':'Atualizar mecanismo.bat';$('engine-status').textContent=`Mecanismo: ${session.engine?'pronto':'ausente'} · FFmpeg: ${session.ffmpeg?'pronto':'não encontrado'} · Runtime: ${session.runtime?'pronto':'não encontrado'}`;
 applyPreferences(session.preferences||{default_quality:defaultQuality},true);renderUpdate({state:'idle',current_version:session.version});await refreshUpdate();checkForUpdates(true);refreshDesktop();refreshRestorePoints();
 if(!session.engine)message('O mecanismo está ausente. Execute '+(session.platform==='linux'?'bash "Iniciar Keep.sh"':'Iniciar Keep.bat')+' para preparar os componentes.',true);
 else if(!session.ffmpeg)message(session.platform==='linux'?'Instale o FFmpeg conforme LEIA-ME-LINUX.txt e reabra o Keep.':'O FFmpeg está ausente. Abra Iniciar Keep.bat para reparar os componentes.');
 showCookieStatus(await api('cookies'));await refresh();showReleaseNotes(true);refreshTimer=setInterval(refresh,1600);
 }catch{$('connection').innerHTML='<span></span>Mecanismo indisponível';message('Inicie o Keep pelo iniciador do seu sistema e depois atualize esta página.',true);}
}
init();

$('repair-captions').addEventListener('click',async()=>{
 $('repair-captions').disabled=true;
 try{const data=await api('subtitles/repair',{});$('captions-status').textContent=data.message;}
 catch(error){$('captions-status').textContent=error.message;}
 finally{$('repair-captions').disabled=false;}
});
$('local-video').addEventListener('change',async()=>{
 const file=$('local-video').files[0];if(!file)return;
 if(!local||!token){$('import-status').textContent='Abra o Keep no computador para importar vídeos.';return;}
 if(searching||startingDownload||batchRunning)return;
 invalidateVideo();const generation=previewGeneration;searching=true;$('inspect').disabled=true;$('local-video').disabled=true;
 $('import-status').textContent='Importando e preparando a prévia… O original permanece na sua pasta.';
 try{
  const response=await fetch('/api/import',{method:'POST',headers:{'X-Keep-Token':token,'X-Keep-Filename':encodeURIComponent(file.name),'Content-Type':'application/octet-stream'},body:file});
  const data=await response.json();if(!response.ok)throw new Error(data.error||'Não foi possível importar este vídeo.');
  if(generation!==previewGeneration){api('preview/cancel',{id:data.preview.id}).catch(()=>{});return;}
  $('video-url').value='';inspection=null;currentItems=[{...data.video,clip:{id:data.preview.id}}];selectedVideos=new Set(['1']);
  showCurrentVideo(currentItems[0]);renderPostVideos();$('video-result').hidden=false;
  showPreview(data.preview,previewGeneration);$('prepare-preview').disabled=true;$('cancel-preview').hidden=true;
  $('import-status').textContent='Vídeo importado. Use a prévia e clique em Baixar para salvar uma versão editada.';
 }catch(error){$('import-status').textContent=error.message;}
 finally{searching=false;$('inspect').disabled=false;$('local-video').disabled=false;$('local-video').value='';}
});

$('choose-local-video').addEventListener('click',()=>$('local-video').click());
for(const key of ['size','x','y'])$('circle-'+key).addEventListener('input',updateVertical);

let circleDrag=null;
$('vertical-preview').addEventListener('pointerdown',event=>{
 if(!verticalEnabled()||editorLayout!=='circle')return;
 const geometry=currentCircleGeometry();if(!geometry)return;
 const box=event.target.getBoundingClientRect(),scale=box.width/geometry.width,d=geometry.diameter*scale,x=geometry.left*scale,y=geometry.top*scale;
 if(Math.hypot(event.clientX-box.left-x-d/2,event.clientY-box.top-y-d/2)>d/2)return;
 event.preventDefault();event.target.setPointerCapture(event.pointerId);
 circleDrag={id:event.pointerId,x:event.clientX,y:event.clientY,px:Number($('circle-x').value)/100,py:Number($('circle-y').value)/100,tw:box.width-d,th:box.height-d};
});
$('vertical-preview').addEventListener('pointermove',event=>{
 if(!circleDrag||event.pointerId!==circleDrag.id)return;
 const clamp=v=>Math.max(0,Math.min(1,v));
 $('circle-x').value=String(clamp(circleDrag.px+(event.clientX-circleDrag.x)/circleDrag.tw)*100);
 $('circle-y').value=String(clamp(circleDrag.py+(event.clientY-circleDrag.y)/circleDrag.th)*100);updateVertical();
});
for(const type of ['pointerup','pointercancel','lostpointercapture'])$('vertical-preview').addEventListener(type,()=>{circleDrag=null;});
