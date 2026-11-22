'use strict';
// Go Video VK provider. GPL-3.0-or-later. No credentials are compiled into the app.
const fs = require('fs'), path = require('path'), query = require('querystring');
const VID = /^-?[1-9][0-9]{0,19}_[1-9][0-9]{0,19}$/;
const OWNER = /^-?[1-9][0-9]{0,19}$/;
const ALBUM = /^(-?[1-9][0-9]{0,19})_(-?[0-9]{1,20})$/;
function mkdir(dir) { if (fs.existsSync(dir)) return; mkdir(path.dirname(dir)); try { fs.mkdirSync(dir, 0o700); } catch(e) { if (e.code !== 'EEXIST') throw e; } }
function atomic(file, value) {
    mkdir(path.dirname(file));
    const temp = file + '.tmp';
    fs.writeFileSync(temp, JSON.stringify(value), { mode: 0o600 });
    fs.chmodSync(temp, 0o600); fs.renameSync(temp, file);
}
function read(file, fallback) { try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch (e) { return fallback; } }
function errorText(e) { return e && e.userMessage || 'Не удалось получить данные VK. Проверьте интернет и права доступа.'; }
function parseLink(text) {
    if (typeof text !== 'string' || text.length > 2048) return null;
    if (VID.test(text)) return text;
    const found = /https?:\/\/[^\s<>]+/i.exec(text), raw = found ? found[0] : /^(?:www\.|m\.)?(?:vkvideo\.ru|vk\.com|vk\.ru)\//i.test(text) ? 'https://' + text : null;
    if (!raw) return undefined;
    const u = require('url').parse(raw, true);
    if (u.auth || u.port || ['vkvideo.ru','www.vkvideo.ru','vk.com','www.vk.com','m.vk.com','vk.ru','www.vk.ru'].indexOf(String(u.hostname).toLowerCase()) < 0) return null;
    let m = /^\/(?:video|clip)(-?[1-9][0-9]{0,19}_[1-9][0-9]{0,19})\/?$/.exec(u.pathname);
    if (!m && u.pathname === '/video_ext.php' && OWNER.test(String(u.query.oid)) && /^[1-9][0-9]{0,19}$/.test(String(u.query.id))) return u.query.oid + '_' + u.query.id;
    if (!m && typeof u.query.z === 'string') m = /^video(-?[1-9][0-9]{0,19}_[1-9][0-9]{0,19})(?:\/|$)/.exec(u.query.z);
    return m ? m[1] : null;
}
class VKAccount {
    constructor(daemon, emit) {
        this.d = daemon; this.emit = emit; this.gen = 0; this.pending = null;
        this.file = path.join(daemon.opts.stateDir, 'vk-account.json');
        this.favoritesFile = path.join(daemon.opts.stateDir, 'favorites.json');
        this.homeFile = path.join(daemon.opts.stateDir, 'vk-home.json');
        this.cred = read(this.file, null); this.catalog = Object.create(null);
        this.queue = Promise.resolve(); this.lastAt = 0;
    }
    status() { return { ev: 'auth', state: this.cred && this.cred.access_token ? 'signed_in' : 'signed_out' }; }
    async token() {
        if (!this.cred) return null;
        if (this.cred.refresh_token && this.cred.expires_at && Date.now() + 60000 >= this.cred.expires_at) {
            if (!this.refreshing) this.refreshing = this.refresh().then(() => { this.refreshing=null; }, (e) => { this.refreshing=null; throw e; });
            await this.refreshing;
        }
        return this.cred && this.cred.access_token || null;
    }
    async start() {
        this.pending = null;
        const config = read(path.join(this.d.opts.stateDir, 'oauth-config.json'), null);
        try {
            const request = require('./oauth').begin(config);
            const remote = config.callback_page && this.d.rc.state().url;
            const url = remote ? config.redirect_uri + '#start=' + encodeURIComponent(
                JSON.stringify({ authorize: request.url, remote })) : request.url;
            this.pending = request.pending;
            this.emit({ ev: 'auth', state: 'code', url, flow: remote ? 'automatic' : 'manual', expires: 600 });
        } catch (e) {
            this.emit({ ev: 'auth', state: 'error', msg: e.message });
        }
    }
    oauth(fields) {
        return new Promise((resolve,reject)=>{
            const child=this.d.spawnFetchUid(this.d.opts.fetcher,['oauth','--cache-dir',this.d.ytc(),'--lease',this.d.opts.leaseFile],true);
            let text='',done=false;
            const finish=(e,v)=>{if(done)return;done=true;clearTimeout(timer);if(e){try{child.kill('SIGKILL');}catch(x){}reject(e);}else resolve(v);};
            const timer=setTimeout(()=>finish(new Error('VK ID не ответил вовремя')),16000);
            child.stdout.on('data',(b)=>{text+=b.toString('utf8');if(Buffer.byteLength(text)>65536)finish(new Error('Некорректный ответ VK ID'));});
            child.on('error',()=>finish(new Error('Не удалось запустить службу входа')));
            child.on('close',(code)=>{let v;try{v=JSON.parse(text);}catch(e){}if(code || !v || v.error)finish(new Error('VK ID отклонил запрос входа'));else finish(null,v);});
            child.stdin.on('error',()=>{});child.stdin.end(JSON.stringify({fields}));
        });
    }
    async finish(callback) {
        const pending=this.pending;
        const request=require('./oauth').finish(pending,callback);
        // Consume before the request: an expired or failed code is never replayed.
        this.pending=null;
        const generation=this.gen;
        const value=await this.oauth(Object.assign({},request.fields,{code:request.code}));
        if (generation!==this.gen || value.state!==pending.state) throw new Error('Ответ VK ID не подтверждён');
        if (this.pending) throw new Error('VK ID request replaced');
        return this.importToken(value.access_token, {
            refresh_token: value.refresh_token, device_id: request.fields.device_id,
            client_id: pending.client_id, redirect_uri: pending.redirect_uri,
            expires_at: Date.now() + Math.max(0, Number(value.expires_in) || 0) * 1000
        });
    }
    async refresh() {
        const original=this.cred,generation=this.gen,state=require('crypto').randomBytes(24).toString('hex');
        const value=await this.oauth({grant_type:'refresh_token',client_id:original.client_id,redirect_uri:original.redirect_uri,device_id:original.device_id,refresh_token:original.refresh_token,state});
        if (generation!==this.gen || value.state!==state || !value.access_token) throw new Error('Обновление сессии VK не подтверждено');
        this.cred=Object.assign({},original,{access_token:value.access_token,refresh_token:value.refresh_token || original.refresh_token,expires_at:Date.now()+Math.max(0,Number(value.expires_in)||0)*1000});
        atomic(this.file,this.cred);
    }
    async importToken(token, session = {}) {
        if (typeof token !== 'string' || token.length < 16 || token.length > 8192 || /[\s\x00-\x1f]/.test(token)) throw new Error('Invalid VK token');
        const generation=this.gen;
        try {
            const user = await this.api('users.get', {}, token);
            if (!Array.isArray(user) || !user[0] || !Number.isSafeInteger(user[0].id)) throw new Error('VK account could not be verified');
            // Verify the actual video permission, not just a successful VK ID profile lookup.
            await this.api('video.get', { owner_id: user[0].id, count: 1 }, token);
            if (generation!==this.gen) throw new Error('VK account changed');
            const credential=Object.assign({},session,{access_token:token,user_id:user[0].id});atomic(this.file,credential);this.cred=credential;this.gen++;
            this.emit(this.status()); return true;
        } catch (e) { throw e; }
    }
    signOut() {
        this.gen++; this.cred = null; this.pending=null; this.catalog = Object.create(null);
        try { fs.unlinkSync(this.file); } catch (e) { if (e.code !== 'ENOENT') throw e; }
        this.emit(this.status());
    }
    close() { this.gen++;this.pending=null; }
    web(operation, q) {
        const work=(this.webQueue||Promise.resolve()).catch(()=>{}).then(()=>this.webRequest(operation,q));
        this.webQueue=work.catch(()=>{});return work;
    }
    webRequest(operation, q) {
        return new Promise((resolve,reject)=>{
            const child=this.d.spawnFetchUid(this.d.opts.fetcher,['web','--cache-dir',this.d.ytc(),'--lease',this.d.opts.leaseFile].concat(this.d.pyzArgs()),true);
            let output='',done=false;
            const finish=(err,value)=>{if(done)return;done=true;clearTimeout(timer);if(err){try{child.kill('SIGKILL');}catch(e){}reject(Object.assign(err,{userMessage:'Не удалось загрузить каталог VK. Проверьте интернет и повторите запрос.'}));}else resolve(value);};
            const timer=setTimeout(()=>finish(new Error('VK web timeout')),60000);
            child.stdout.on('data',b=>{output+=b.toString('utf8');if(Buffer.byteLength(output)>8*1024*1024)finish(new Error('VK response too large'));});
            child.on('error',()=>finish(new Error('VK helper unavailable')));
            child.on('close',code=>{let value;try{value=JSON.parse(output);}catch(e){}if(code||!value||!Array.isArray(value.items))finish(new Error('VK web failed'));else finish(null,value);});
            child.stdin.on('error',()=>{});child.stdin.end(JSON.stringify({operation,q:q||''}));
        });
    }
    json() { return Promise.reject(new Error('YouTube request unavailable in VK provider')); }
    async api(method, params, tokenOverride) {
        const token = tokenOverride || await this.token();
        if (!token) throw Object.assign(new Error('VK login required'), { userMessage: 'Личные разделы VK пока недоступны в тестовой версии. Используйте каталог, поиск и локальное избранное.' });
        if (!/^[a-z]+\.[A-Za-z]+$/.test(method)) throw new Error('Invalid VK method');
        const generation = this.gen;
        const work = this.queue.catch(() => {}).then(async () => {
            const wait = Math.max(0, this.lastAt + 350 - Date.now());
            if (wait) await new Promise((r) => setTimeout(r, wait));
            if (generation !== this.gen) throw new Error('VK account changed');
            this.lastAt = Date.now();
            // The request travels to the unprivileged network child on stdin. Never log its URL.
            const r = await this.d.fetch({ method: 'GET', url: 'https://api.vk.com/method/' + method + '?' + query.stringify(Object.assign({}, params, { v: '5.199', access_token: token })), timeoutMs: 12000 });
            if (generation !== this.gen) throw new Error('VK account changed');
            let b; try { b = JSON.parse(r.body); } catch (e) { throw new Error('VK returned invalid JSON'); }
            if (r.status !== 200 || b.error) {
                const code = b.error && Number(b.error.error_code);
                const messages = { 5: 'Сессия VK истекла. Выполните вход заново.', 6: 'VK ограничил частоту запросов. Повторите позже.', 7: 'Приложению VK не предоставлено разрешение на видео.', 15: 'VK отказал в доступе. Проверьте права приложения и доступность видео.', 14: 'VK запросил проверку пользователя. Откройте VK на телефоне.', 18: 'Аккаунт VK недоступен.' };
                throw Object.assign(new Error('VK API error ' + (code || r.status)), { userMessage: messages[code] || 'VK вернул ошибку ' + (code || r.status) });
            }
            if (!Object.prototype.hasOwnProperty.call(b, 'response')) throw new Error('VK response missing');
            return b.response;
        });
        this.queue = work.catch(() => {}); return work;
    }
    items(result) {
        const names = Object.create(null);
        (result.profiles || []).forEach((p) => { names[String(p.id)] = [p.first_name,p.last_name].filter(Boolean).join(' '); });
        (result.groups || []).forEach((p) => { names['-' + p.id] = p.name; });
        return (result.items || []).map((v) => {
            const id = String(v.owner_id) + '_' + String(v.id);
            if (!VID.test(id) || v.live_status === 'started' || v.processing) return null;
            const pictures = (v.image || []).slice().sort((a,b) => a.width - b.width);
            const picture = pictures.find((p) => p.width >= 320) || pictures[pictures.length - 1];
            const item = { id, title: String(v.title || 'VK Видео').slice(0,300), channel: names[String(v.owner_id)] || 'VK Видео', duration_s: Math.max(0,Number(v.duration) || 0), meta: v.views ? [String(v.views) + ' просмотров'] : [] };
            this.catalog[id] = Object.assign({}, item, { thumbnail: picture && picture.url });
            return item;
        }).filter(Boolean);
    }
    async all(method, params, max) {
        let items = [], result = {}, count = 100; max = max || 1000;
        while (items.length < max) {
            const page = await this.api(method, Object.assign({}, params, { count, offset: items.length, extended: 1 }));
            if (!page || !Array.isArray(page.items)) throw new Error('Invalid VK list');
            items = items.concat(page.items); result = Object.assign({}, result, page);
            if (page.items.length < count || items.length >= page.count) break;
        }
        result.items = items.slice(0,max); return result;
    }
    async home(generation) {
        const cached = read(this.homeFile, []);
        const rows = Array.isArray(cached) ? cached.filter((row) => row && VID.test(row.id)).slice(0, 100) : [];
        const send = (items, extra) => {
            if (generation === this.gen) this.emit(Object.assign({ ev: 'feed', which: 'home', items }, extra));
        };
        const previous = rows.map(({ thumbnail, ...item }) => {
            if (!this.catalog[item.id]) this.catalog[item.id] = Object.assign({ thumbnail }, item);
            return item;
        });
        if (previous.length) send(previous, { partial: true });
        if (!this.homeRefresh) {
            this.homeRefresh = this.web('catalog').then((result) => {
                if (generation !== this.gen) return null;
                const items = this.items(result);
                if (items.length) {
                    try { atomic(this.homeFile, items.map((item) => this.catalog[item.id])); }
                    catch (e) { this.d.log('VK: could not write catalog cache'); }
                }
                return items;
            }).finally(() => { this.homeRefresh = null; });
        }
        try {
            const fresh = await this.homeRefresh;
            if (fresh) send(fresh.length ? fresh : previous);
        } catch (e) {
            if (previous.length) send(previous);
            else send([], { error: errorText(e) });
        }
    }
    async feed(which) {
        const generation = this.gen;
        if (which === 'home') return this.home(generation);
        try {
            let items;
            if (which === 'hist') items = Object.keys(this.d.positions()).sort((a,b)=>(this.d.positions()[b].at||0)-(this.d.positions()[a].at||0)).map((id) => Object.assign({ id }, this.d.positions()[id])).filter((v) => VID.test(v.id)).slice(0,100);
            else if (which === 'wl') items = read(this.favoritesFile, []).filter((v) => VID.test(v.id));
            else if(which==='subs' && !this.cred) throw Object.assign(new Error('Guest mode'),{userMessage:'Подписки требуют личного входа. В тестовой версии доступны каталог, поиск, локальное избранное и история.'});
            else if(which==='subs') {
                const groups=await this.all('groups.get',{extended:1},100);
                items=[];
                for(const group of groups.items.slice(0,8)) {
                    try { items=items.concat(this.items(await this.api('video.get',{owner_id:-group.id,count:12,extended:1}))); }
                    catch(e) { /* one unavailable community must not discard the others */ }
                }
            } else items = this.items(await this.all('video.get', { owner_id: this.cred && this.cred.user_id }));
            if (generation === this.gen) this.emit({ ev: 'feed', which, items });
        } catch (e) { if (generation === this.gen) this.emit({ ev: 'feed', which, error: errorText(e), items: [] }); }
    }
    async channels() {
        try {
            const r = await this.all('groups.get', { extended: 1 });
            this.emit({ ev: 'channels', items: r.items.map((g) => ({ id: '-' + g.id, title: g.name, handle: g.screen_name || '' })) });
        } catch (e) { this.emit({ ev: 'channels', items: [], error: errorText(e) }); }
    }
    async channel(id) {
        if (!OWNER.test(String(id))) return this.emit({ ev:'channel', id, items:[], error:'Некорректный канал VK' });
        try { this.emit({ ev:'channel', id, items:this.items(await this.all('video.get',{ owner_id:id })) }); }
        catch (e) { this.emit({ ev:'channel', id, items:[], error:errorText(e) }); }
    }
    async playlists() {
        try {
            const r = await this.all('video.getAlbums', { owner_id: this.cred && this.cred.user_id, need_system: 1 });
            this.emit({ ev:'playlists', items:r.items.map((a) => ({ id:String(a.owner_id)+'_'+a.id, title:String(a.title || 'Альбом'), count:a.count || 0 })) });
        } catch (e) { this.emit({ ev:'playlists', items:[], error:errorText(e) }); }
    }
    async playlist(id) {
        const m = ALBUM.exec(String(id));
        if (!m) return this.emit({ ev:'playlist', id, items:[], error:'Некорректный альбом VK' });
        try { this.emit({ ev:'playlist', id, items:this.items(await this.all('video.get',{ owner_id:m[1], album_id:m[2] })) }); }
        catch (e) { this.emit({ ev:'playlist', id, items:[], error:errorText(e) }); }
    }
    favorite(id) {
        if (!VID.test(id)) throw new Error('Invalid video id');
        const old = read(this.favoritesFile, []), had = old.some((x) => x.id === id);
        const item = this.catalog[id] || Object.assign({ id, title:'VK Видео' }, this.d.positions()[id]);
        atomic(this.favoritesFile, had ? old.filter((x) => x.id !== id) : [item].concat(old).slice(0,1000));
        return this.feed('wl');
    }
}
function install(Daemon) {
    // The original playback state machine, audio release, decoder and reconnection stay in RideTube.
    const original = Daemon.prototype.onLine;
    Daemon.prototype.onLine = function(line) {
        let m; try { m = JSON.parse(line); } catch(e) { return original.call(this,line); }
        if (m && m.cmd === 'favorite') return this.account.favorite(m.id);
        if (m && m.cmd === 'auth_finish') return this.account.finish(m.url).catch((e)=>this.emit({ev:'auth',state:'error',msg:errorText(e)}));
        return original.call(this,line);
    };
    Daemon.prototype.search = async function (q) {
        q = String(q || '').trim().slice(0,200);
        const id = parseLink(q);
        if (id) {
            if (this.account.cred) {
                try {
                    const items=this.account.items(await this.account.api('video.get',{videos:id,extended:1}));
                    if(items.length)return this.emit({ev:'results',q,items});
                } catch(e) { /* a public link can still be played without the account's catalog */ }
            }
            return this.emit({ ev:'results', q, items:[this.account.catalog[id] || { id, title:'VK Видео · '+id, channel:'VK Видео', duration_s:0 }] });
        }
        if (id === null) return this.emit({ ev:'results', q, items:[], error:'Ссылка должна вести на ролик VK Видео.' });
        try { this.emit({ ev:'results', q, items:this.account.items(await this.account.web('search',q)) }); }
        catch (e) { this.emit({ ev:'results', q, items:[], error:errorText(e) }); }
    };
    Daemon.prototype.related = function (id) { return this.emit({ ev:'related', id, items:[] }); };
    Daemon.prototype.loadSegments = function(c) { c.segs=[];this.sendExtras();return Promise.resolve(); };
    Daemon.prototype.trackStart = function () { return Promise.resolve(); };
    Daemon.prototype.ping = function () { return Promise.resolve(); };
    Daemon.prototype.autoTick = function () { return Promise.resolve(); };
    Daemon.prototype.sendLogs = function () { return this.emit({ ev:'logs_error', msg:'closed' }); };
}
module.exports = { VKAccount, install, parseLink, VID, errorText, atomic };
