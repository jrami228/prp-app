// Loads the stored data files (data/*.json) with RELATIVE paths, so the app works at a domain
// root or under a GitHub Pages project subpath (e.g. /PRP.github.io/). Then loads js/app.js.
// NOTE: browsers block fetch() from file:// pages. Test with a local web server (see README).
(function () {
    'use strict';
    var FILES = {
        meta: 'data/meta.json',
        utMapping: 'data/ut-mapping.json',
        folios: 'data/folios.json',
        facilities: 'data/facilities.json',
        eligibility: 'data/eligibility.json',
        sharedAddress: 'data/shared-address.json',
        facidTasks: 'data/facid-tasks.json',
        icr: 'data/icr.json'
    };
    // Files the app can run without (features degrade gracefully).
    var OPTIONAL = { icr: true, meta: true };

    function showLoadError(msg) {
        var el = document.getElementById('data-status');
        if (el) {
            el.className = 'text-xs text-center mt-3 px-3 py-2 rounded-lg bg-red-900/60 border border-red-500 text-red-100';
            el.textContent = msg;
        }
    }

    var keys = Object.keys(FILES);
    var failed = [];
    Promise.all(keys.map(function (k) {
        return fetch(FILES[k], { cache: 'no-cache' })
            .then(function (r) { if (!r.ok) throw new Error(r.status + ' ' + FILES[k]); return r.json(); })
            .catch(function (e) { failed.push(FILES[k]); return OPTIONAL[k] ? null : undefined; });
    })).then(function (results) {
        var data = {};
        keys.forEach(function (k, i) { data[k] = results[i]; });
        window.APP_DATA = data;
        window.APP_DATA_FAILED = failed.slice();
        var required = keys.filter(function (k) { return !OPTIONAL[k] && data[k] === undefined; });
        if (required.length) {
            var fileProto = location.protocol === 'file:';
            showLoadError('Some stored data could not be loaded (' + failed.join(', ') + '). ' +
                (fileProto ? 'Pages opened directly from disk (file://) cannot load data files; run a local web server (see README).'
                           : 'Searches that rely on stored data may be incomplete; live FDEP links still work.'));
        }
        var s = document.createElement('script');
        s.src = 'js/app.js';
        document.body.appendChild(s);
    });
})();
