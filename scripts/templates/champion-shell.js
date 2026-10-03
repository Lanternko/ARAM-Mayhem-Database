// Restore the shared app body in-place. The champion URL and its SEO head stay intact.
(async () => {
    const shellUrl = document.currentScript.dataset.appShell;
    const response = await fetch(shellUrl);
    if (!response.ok) throw new Error(`App shell: ${response.status}`);
    const shell = new DOMParser().parseFromString(await response.text(), 'text/html');
    const scripts = [...shell.body.querySelectorAll('script')];
    scripts.forEach(script => script.remove());
    const fallback = document.getElementById('champion-fallback');
    const host = shell.getElementById('champ-page-host');
    if (!host || !fallback) throw new Error('Missing champion shell host');
    host.innerHTML = fallback.innerHTML;
    shell.getElementById('view-home')?.classList.remove('is-active');
    shell.getElementById('view-champ')?.classList.add('is-active');
    fallback.replaceWith(...shell.body.childNodes);
    // Inline build config must precede the cached site.js startup.
    for (const source of scripts) {
        const script = document.createElement('script');
        for (const attr of source.attributes) script.setAttribute(attr.name, attr.value);
        script.textContent = source.textContent;
        if (source.src) {
            await new Promise((resolve, reject) => {
                script.onload = resolve;
                script.onerror = reject;
                document.body.append(script);
            });
        } else {
            document.body.append(script);
        }
    }
})().catch(error => {
    console.error(error);
    const status = document.getElementById('champion-load-status');
    const language = document.documentElement.lang;
    if (status) status.textContent = language === 'en'
        ? 'Could not load the interactive view. Reload or use the tier-list link.'
        : language === 'zh-Hans'
            ? '互动页面载入失败，请重新整理或使用英雄榜连结。'
            : '互動頁面載入失敗，請重新整理或使用英雄榜連結。';
});
