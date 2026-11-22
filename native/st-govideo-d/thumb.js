'use strict';
// node22 thumb.js <id> [--lease <path>]: i.ytimg.com's mqdefault.jpg as 320x180 RGBA (230400 bytes) on stdout.
// Runs as the fetch uid (env {}, cwd /); the car has no image library, so the JPEG is decoded here.
const https = require('https');
const decode = require('./vendor/jpeg-js/decoder.js');

const W = 320, H = 180, MAX = 2 * 1024 * 1024, TIMEOUT_MS = 8000;

// as ytd.js's uplinkLookup (Node 8 there); Node 22's autoSelectFamily asks with {all: true}
function uplinkLookup(leaseFile) {
    let servers = [];
    try { servers = (JSON.parse(require('fs').readFileSync(leaseFile || '/run/mgu-uplink/lease.json', 'utf8')).dns || []).filter((x) => typeof x === 'string'); } catch (e) { /* no lease: system resolver */ }
    if (!servers.length) return undefined;
    const r = new (require('dns').Resolver)();
    r.setServers(servers);
    return function (host, opts, cb) {
        if (typeof opts === 'function') { cb = opts; opts = {}; }
        r.resolve4(host, (e, a) => (e ? cb(e) : opts.all ? cb(null, a.map((address) => ({ address, family: 4 }))) : cb(null, a[0], 4)));
    };
}

function fetchJpeg(source, lookup) {
    const parsed=require('url').parse(source);
    const host=String(parsed.hostname || '').toLowerCase();
    if(parsed.protocol!=='https:' || parsed.auth || parsed.port || !/(^|\.)(userapi\.com|vkuserphoto\.ru|vkvideo\.ru|vk\.com|vk\.ru)$/.test(host)) return Promise.reject(new Error('unsupported thumbnail host'));

    return new Promise((resolve, reject) => {
        const req = https.request({ host: host, path: parsed.path, lookup, headers: { 'user-agent': 'Mozilla/5.0' } }, (res) => {
            // 404 is YouTube's placeholder for private/removed videos; redirects are not followed
            if (res.statusCode !== 200) { res.resume(); return reject(new Error('http ' + res.statusCode)); }
            const parts = [];
            let n = 0;
            res.on('data', (d) => { n += d.length; if (n > MAX) req.destroy(new Error('body over 2 MB')); else parts.push(d); });
            res.on('end', () => resolve(Buffer.concat(parts)));
            res.on('error', reject);
        });
        req.setTimeout(TIMEOUT_MS, () => req.destroy(new Error('timeout')));
        setTimeout(() => req.destroy(new Error('timeout')), TIMEOUT_MS).unref();
        req.on('error', reject);
        req.end();
    });
}

function thumbRgba(jpeg) {
    const img = decode(jpeg, { useTArray: true, formatAsRGBA: true, maxResolutionInMP: 4, maxMemoryUsageInMB: 64 });
    const out = Buffer.alloc(W * H * 4);
    for (let y = 0; y < H; y++) {
        const sy = Math.floor((y + 0.5) * img.height / H);
        for (let x = 0; x < W; x++) {
            const s = (sy * img.width + Math.floor((x + 0.5) * img.width / W)) * 4;
            const d = (y * W + x) * 4;
            out[d] = img.data[s]; out[d + 1] = img.data[s + 1]; out[d + 2] = img.data[s + 2]; out[d + 3] = 255;
        }
    }
    return out;
}

module.exports = { thumbRgba, uplinkLookup };

if (require.main === module) {
    const fail=()=>{process.stderr.write('VK thumbnail unavailable\n');process.exit(1);};
    const a=process.argv.slice(2), id=a[0], src=a.indexOf('--source'), cache=a.indexOf('--cache-dir'), lease=a.indexOf('--lease');
    if(!/^-?[1-9][0-9]{0,19}_[1-9][0-9]{0,19}$/.test(id || '')) fail();
    const emit=(source)=>fetchJpeg(source,uplinkLookup(lease>=0?a[lease+1]:undefined)).then((jpeg)=>process.stdout.write(thumbRgba(jpeg),()=>process.exit(0)),fail);
    const choose=(source)=>{if(source) return emit(source);
    {
        const cp=require('child_process'),path=require('path');
        const args=['-I',path.join(__dirname,'vkfetch.py'),'info','--cache-dir',cache>=0?a[cache+1]:'/var/opt/st-govideo-d/cache/ytdlp','--',id];
        cp.execFile('/var/opt/mgu-python/current/bin/python3',args,{timeout:8000,maxBuffer:65536},(e,out)=>{
            if(e)return fail();let value;try{value=JSON.parse(out);}catch(x){return fail();}
            emit(value.thumbnail || '');
        });
    }};
    let input='';process.stdin.setEncoding('utf8');process.stdin.on('data',b=>{input+=b;if(input.length>8192)fail();});
    process.stdin.on('end',()=>{let v;try{v=JSON.parse(input || '{}');}catch(e){return fail();}choose(typeof v.source==='string'?v.source:'');});
}
