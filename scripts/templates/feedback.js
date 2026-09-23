(() => {
    const root = document.documentElement;
    const locale = root.dataset.feedbackLocale;
    const form = document.querySelector('[data-feedback-form]');
    const themeButton = document.querySelector('[data-theme-toggle]');
    const syncTheme = () => {
        const light = root.dataset.theme === 'light';
        const labels = { zh: ['切換淺色', '切換深色'], 'zh-CN': ['切换浅色', '切换深色'], en: ['Switch to light theme', 'Switch to dark theme'] };
        const label = labels[locale][light ? 1 : 0];
        themeButton.title = label;
        themeButton.setAttribute('aria-label', label);
    };
    syncTheme();
    themeButton.addEventListener('click', () => {
        root.dataset.theme = root.dataset.theme === 'light' ? 'dark' : 'light';
        try { localStorage.setItem('aram-mayhem-site-theme', root.dataset.theme); } catch {}
        syncTheme();
    });
    // Keep unsent text private and scoped to this tab when changing language.
    const draftKey = 'aram-feedback-draft';
    try {
        const draft = JSON.parse(sessionStorage.getItem(draftKey) || 'null');
        if (draft) {
            form.elements.message.value = draft.message || '';
            form.elements.contact_email.value = draft.email || '';
            form.elements.contact_consent.checked = draft.consent === true;
            form.elements.contact_email.dispatchEvent(new Event('input'));
        }
        sessionStorage.removeItem(draftKey);
    } catch {}
    document.querySelectorAll('[data-lang]').forEach(button => {
        button.addEventListener('click', () => {
            const lang = button.dataset.lang;
            if (lang === locale) { document.getElementById('lang-menu').open = false; return; }
            try {
                sessionStorage.setItem(draftKey, JSON.stringify({
                    message: form.elements.message.value,
                    email: form.elements.contact_email.value,
                    consent: form.elements.contact_consent.checked
                }));
                localStorage.setItem('aram-mayhem-site-lang', lang);
            } catch {}
            location.assign((lang === 'zh' ? '' : '/' + lang) + '/feedback/');
        });
    });
    document.addEventListener('keydown', event => {
        if (event.key !== 'Escape') return;
        document.querySelectorAll('.site-header details[open]').forEach(menu => {
            menu.open = false;
            menu.querySelector('summary').focus();
        });
    });
    document.addEventListener('click', event => {
        document.querySelectorAll('.site-header details[open]').forEach(menu => {
            if (!menu.contains(event.target)) menu.open = false;
        });
    });
})();
