(function () {
  'use strict';

  var PLUGIN_ID = 'stashapi';

  var ROUTES = [
    { path: '/plugins/stashapi/shell',    url: '/plugin/stashapi/assets/shell.html',    title: 'Remote Shell' },
    { path: '/plugins/stashapi/explorer', url: '/plugin/stashapi/assets/explorer.html', title: 'File Explorer' }
  ];

  // Warm up the terminal server as soon as the Stash UI loads,
  // so the web terminal connects instantly.
  function warmup() {
    try {
      var gql = function () {
        return fetch('/graphql', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'same-origin',
          body: JSON.stringify({ query: 'mutation($p:ID!,$a:Map!){runPluginOperation(plugin_id:$p,args:$a)}',
                                 variables: { p: PLUGIN_ID, a: { action: 'hello' } } }),
        });
      };
      gql().catch(function () { setTimeout(gql, 3000); });
    } catch (e) { /* ignore */ }
  }

  function matches(pathname) {
    var p = pathname.replace(/\/+$/, '');
    for (var i = 0; i < ROUTES.length; i++) {
      if (p === ROUTES[i].path) return true;
    }
    return false;
  }

  function nudge(times) {
    if (times <= 0) return;
    if (!matches(window.location.pathname)) return;  // user left the page  stop poking the router
    try {
      window.history.replaceState(window.history.state, '', window.location.href);
    } catch (e) { /* ignore */ }
    try {
      window.dispatchEvent(new PopStateEvent('popstate', { state: window.history.state }));
    } catch (e) {
      try { window.dispatchEvent(new Event('popstate')); } catch (_) {}
    }
    setTimeout(function () { nudge(times - 1); }, 250);
  }

  function register() {
    var P = window.PluginApi;
    if (!P) return setTimeout(register, 100);

    if (!P.register || typeof P.register.route !== 'function') {
      console.warn('[stashapi] PluginApi.register.route     Stash. ' +
        '  : ' + ROUTES.map(function (r) { return r.url; }).join(' , '));
      return;
    }

    var React = P.React || window.React;
    if (!React) return setTimeout(register, 100);

    ROUTES.forEach(function (r) {
      P.register.route({
        path: r.path,
        component: function () {
          return React.createElement('iframe', {
            key: r.path,
            src: r.url,
            title: r.title,
            style: {
              display: 'block',
              width: '100%',
              height: 'calc(100vh - 56px)',
              border: '0',
              background: '#12141a'
            }
          });
        }
      });
    });

    console.log('[stashapi]  ');

    warmup();

    if (matches(window.location.pathname)) nudge(8);
  }

  register();
})();