/* PowerWash CRM — connection settings.
 *
 * Fill these two values in from your Supabase project:
 *   Supabase dashboard -> Project Settings -> API
 *     Project URL  ->  url
 *     anon public  ->  anonKey   (this key is safe to ship in the browser;
 *                                 Row-Level Security is what protects your data)
 *
 * You can also set them at runtime without editing this file by adding
 * ?supabase_url=...&supabase_key=... to the URL once — they'll be remembered
 * in this browser. That makes it easy to point the same build at a test
 * project vs. your live project.
 */
(function (root) {
  var saved = {};
  try { saved = JSON.parse(localStorage.getItem('pwcrm-config') || '{}'); } catch (e) {}

  // one-time override via query string (handy for testing)
  try {
    var q = new URLSearchParams(location.search);
    if (q.get('supabase_url')) saved.url = q.get('supabase_url');
    if (q.get('supabase_key')) saved.anonKey = q.get('supabase_key');
    if (q.get('supabase_url') || q.get('supabase_key')) {
      localStorage.setItem('pwcrm-config', JSON.stringify(saved));
    }
  } catch (e) {}

  root.CONFIG = {
    // ---- EDIT THESE (or pass them once via the URL, see above) ----
    url: saved.url || 'YOUR_SUPABASE_URL',
    anonKey: saved.anonKey || 'YOUR_SUPABASE_ANON_KEY',
    configured: function () {
      return this.url && this.anonKey &&
        this.url.indexOf('YOUR_SUPABASE') === -1 &&
        this.anonKey.indexOf('YOUR_SUPABASE') === -1;
    }
  };
})(typeof self !== 'undefined' ? self : this);
