(() => {
  'use strict';
  const status = document.getElementById('status');
  const query = new URLSearchParams(location.hash.slice(1));
  const origin = query.get('origin'), nonce = query.get('nonce'), platform = query.get('platform');
  const limit = 4 * 1024 * 1024;
  let release, downloading = false;
  let assetFilename = platform === 'windows' ? 'KeepUpdate.zip' : 'KeepLinuxUpdate.tar.gz';
  const say = text => { status.textContent = text; };
  const send = (type, detail = {}, transfer = []) => window.opener.postMessage({type, nonce, ...detail}, origin, transfer);
  if (!window.opener || !/^http:\/\/(127\.0\.0\.1|localhost):\d{1,5}$/.test(origin || '') ||
      !/^[A-Za-z0-9_-]{30,80}$/.test(nonce || '') || !['windows', 'linux'].includes(platform)) {
    say('A página privada foi aberta. Após entrar com sua conta autorizada, volte ao Keep e clique novamente em “Verificar atualização”. Você pode fechar esta aba.');
    return;
  }
  // The fragment is never sent to the server. Clear it after validating so the
  // temporary nonce also does not remain in a copied address or browser history.
  history.replaceState(null, '', location.pathname);
  const fail = () => {
    say('Não foi possível consultar o pacote privado. Volte ao Keep e tente novamente ou importe um pacote recebido.');
    send('keep-update-error');
  };
  window.addEventListener('message', async event => {
    if (event.origin !== origin || event.source !== window.opener || event.data?.nonce !== nonce ||
        event.data?.type !== 'keep-update-download' || !release || downloading) return;
    downloading = true;
    try {
      const item = release.packages[platform];
      const filename = platform === 'windows' ? 'KeepUpdate.zip' : 'KeepLinuxUpdate.tar.gz';
      if (item.file !== filename || !Number.isInteger(item.size) || item.size <= 0 || item.size > limit ||
          !/^[a-f0-9]{64}$/.test(item.sha256)) throw new Error('Pacote inválido');
      say('Recebendo o pacote privado. Aguarde…');
      const response = await fetch('/' + assetFilename, {credentials: 'same-origin', cache: 'no-store', redirect: 'error'});
      if (!response.ok) throw new Error('Sem acesso');
      const reader = response.body.getReader(), chunks = [];
      let size = 0;
      while (true) {
        const next = await reader.read();
        if (next.done) break;
        size += next.value.byteLength;
        if (size > limit || size > item.size) { await reader.cancel(); throw new Error('Pacote grande demais'); }
        chunks.push(next.value);
      }
      if (size !== item.size) throw new Error('Pacote incompleto');
      const data = new Uint8Array(size);
      let offset = 0;
      for (const chunk of chunks) { data.set(chunk, offset); offset += chunk.byteLength; }
      const hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256', data))].map(v => v.toString(16).padStart(2, '0')).join('');
      if (hash !== item.sha256) throw new Error('Verificação falhou');
      send('keep-update-package', {package: data.buffer}, [data.buffer]);
      say('Pacote enviado ao Keep. Volte ao aplicativo para instalar. Você já pode fechar esta janela.');
    } catch (_) { fail(); }
  });
  (async () => {
    try {
      const response = await fetch('/keep-release.json', {credentials: 'same-origin', cache: 'no-store', redirect: 'error'});
      if (!response.ok || +(response.headers.get('content-length') || 0) > 32768) throw new Error('Sem acesso');
      const text = await response.text();
      if (text.length > 32768) throw new Error('Descrição inválida');
      release = JSON.parse(text);
      const current = (query.get('current') || '').split('.').map(Number);
      if (release.compatibility && current.length >= 2 && current.every(Number.isFinite) &&
          current[0] === 1 && current[1] < 8) {
        release = release.compatibility;
        assetFilename = platform === 'windows' ? 'KeepBridgeUpdate.zip' : 'KeepBridgeLinuxUpdate.tar.gz';
      }
      if (!/^\d{1,4}\.\d{1,4}(\.\d{1,4})?$/.test(release.version || '') || !release.packages?.[platform]) throw new Error('Descrição inválida');
      send('keep-update-check', {release});
      say('Versão ' + release.version + ' encontrada. Aguarde a confirmação do Keep.');
    } catch (_) { fail(); }
  })();
})();
