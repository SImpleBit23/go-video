'use strict';
// VK ID OAuth 2.1 + PKCE; flow matches VKCOM/vkid-web-sdk's Auth.exchangeCode.
const crypto = require('crypto'), url = require('url'), qs = require('querystring');
const b64 = (b) => b.toString('base64').replace(/=/g,'').replace(/\+/g,'-').replace(/\//g,'_');
function begin(config, now) {
    if (!config || !/^[1-9][0-9]{0,14}$/.test(String(config.client_id))) throw new Error('Не настроен client_id VK ID');
    const r = url.parse(String(config.redirect_uri || ''));
    if (r.auth || r.hash || r.search || !r.hostname || !(r.protocol === 'https:' || r.protocol === 'http:' && (r.hostname === 'localhost' || r.hostname === '127.0.0.1'))) throw new Error('В кабинете VK ID требуется настроить Redirect URL; затем указать тот же адрес в oauth-config.json');
    const pending = { verifier:b64(crypto.randomBytes(48)), state:b64(crypto.randomBytes(24)), created:now || Date.now(), redirect_uri:config.redirect_uri, client_id:String(config.client_id) };
    const args = { client_id:pending.client_id, redirect_uri:pending.redirect_uri, response_type:'code', state:pending.state,
        code_challenge:b64(crypto.createHash('sha256').update(pending.verifier).digest()), code_challenge_method:'S256', scope:config.scope || 'video' };
    return { pending, url:'https://id.vk.com/authorize?' + qs.stringify(args) };
}
function finish(pending, callback, now) {
    if (!pending || (now || Date.now()) - pending.created > 10 * 60000) throw new Error('Время входа истекло. Запросите новый код.');
    if (typeof callback !== 'string' || callback.length > 8192) throw new Error('Некорректный ответ VK ID');
    const r = url.parse(callback,true), expected = url.parse(pending.redirect_uri);
    if (r.auth || r.hash || r.protocol !== expected.protocol || r.host !== expected.host || r.pathname !== expected.pathname) throw new Error('Адрес ответа не совпадает с Redirect URL');
    if (r.query.state !== pending.state) throw new Error('Ответ VK ID относится к другому запросу входа');
    if (r.query.error) throw new Error('VK ID отменил вход');
    if (typeof r.query.code !== 'string' || !r.query.code || typeof r.query.device_id !== 'string' || !r.query.device_id) throw new Error('VK ID не вернул code или device_id');
    return { endpoint:'https://id.vk.com/oauth2/auth', fields:{ grant_type:'authorization_code', client_id:pending.client_id,
        redirect_uri:pending.redirect_uri, code_verifier:pending.verifier, state:pending.state, device_id:r.query.device_id }, code:r.query.code };
}
module.exports = { begin, finish };
