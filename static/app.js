// Loading Overlay Management
function showLoading(message = 'Loading...') {
    const overlay = document.getElementById('loading-overlay');
    const text = document.getElementById('loading-text');
    if (overlay) {
        if (text) text.textContent = message;
        overlay.classList.add('active');
    }
}

function hideLoading() {
    const overlay = document.getElementById('loading-overlay');
    if (overlay) {
        overlay.classList.remove('active');
    }
}

const SVG_NS = 'http://www.w3.org/2000/svg';
let bracketDrawScheduled = false;

function renderBracketConnector(svg, wrapperRect, bracket, matchEl) {
    const nextRound = matchEl.dataset.nextRound;
    const nextIndex = matchEl.dataset.nextIndex;
    if (!nextRound || !nextIndex) {
        return;
    }

    const target = bracket.querySelector(
        `.bracket-match[data-round="${nextRound}"][data-index="${nextIndex}"]`
    );
    if (!target) {
        return;
    }

    const sourceRect = matchEl.getBoundingClientRect();
    const targetRect = target.getBoundingClientRect();

    const startX = sourceRect.right - wrapperRect.left;
    const startY = sourceRect.top - wrapperRect.top + sourceRect.height / 2;
    const endX = targetRect.left - wrapperRect.left;
    const endY = targetRect.top - wrapperRect.top + targetRect.height / 2;

    const offsetStartX = startX + 8;
    const offsetEndX = Math.max(offsetStartX + 24, endX - 8);
    const controlOffset = Math.max(48, (offsetEndX - offsetStartX) * 0.5);

    const path = document.createElementNS(SVG_NS, 'path');
    const d = `M ${offsetStartX} ${startY} C ${offsetStartX + controlOffset} ${startY}, ${offsetEndX - controlOffset} ${endY}, ${offsetEndX} ${endY}`;
    path.setAttribute('d', d);
    path.classList.add('bracket-connector');
    if (matchEl.dataset.state === 'complete') {
        path.classList.add('is-complete');
    }
    svg.appendChild(path);
}

function drawBracketConnectors() {
    const wrappers = document.querySelectorAll('.bracket-wrapper');

    for (const wrapper of wrappers) {
        const svg = wrapper.querySelector('.bracket-connector-layer');
        const bracket = wrapper.querySelector('.bracket');
        if (!svg || !bracket) {
            continue;
        }

        const matches = bracket.querySelectorAll('.bracket-match[data-round]');
        if (matches.length === 0) {
            svg.innerHTML = '';
            svg.setAttribute('width', 0);
            svg.setAttribute('height', 0);
            continue;
        }

        const wrapperRect = wrapper.getBoundingClientRect();
        const width = Math.max(wrapper.scrollWidth, wrapperRect.width);
        const height = Math.max(wrapper.scrollHeight, wrapperRect.height);

        svg.setAttribute('width', width);
        svg.setAttribute('height', height);
        svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
        svg.innerHTML = '';

        for (const matchEl of matches) {
            renderBracketConnector(svg, wrapperRect, bracket, matchEl);
        }
    }
}

function scheduleBracketDraw() {
    if (bracketDrawScheduled) {
        return;
    }
    bracketDrawScheduled = true;
    globalThis.requestAnimationFrame(() => {
        bracketDrawScheduled = false;
        drawBracketConnectors();
    });
}

// Add loading spinner to forms on submit
document.addEventListener('DOMContentLoaded', function() {
    // Add loading to all forms except search forms
    const forms = document.querySelectorAll('form:not(.no-loading)');
    for (const form of forms) {
        form.addEventListener('submit', function(e) {
            // Don't show loading for GET forms (search)
            if (form.method.toLowerCase() !== 'get') {
                showLoading('Processing...');
            }
        });
    }

    // Player Search/Filter Functionality
    const searchInput = document.getElementById('player-search');
    if (searchInput) {
        searchInput.addEventListener('input', function(e) {
            const searchTerm = e.target.value.toLowerCase();
            const rows = document.querySelectorAll('.searchable-row');

            for (const row of rows) {
                const text = row.textContent.toLowerCase();
                if (text.includes(searchTerm)) {
                    row.style.display = '';
                } else {
                    row.style.display = 'none';
                }
            }
        });
    }

    // Confirmation dialogs for destructive actions
    const dangerButtons = document.querySelectorAll('.btn-danger, [data-confirm]');
    for (const button of dangerButtons) {
        button.addEventListener('click', function(e) {
            const message = button.dataset.confirm || 'Are you sure you want to proceed?';
            if (!confirm(message)) {
                e.preventDefault();
                return false;
            }
        });
    }

    // Auto-hide flash messages after 5 seconds
    const flashMessages = document.querySelectorAll('.flash');
    for (const flash of flashMessages) {
        setTimeout(() => {
            flash.style.transition = 'opacity 0.5s ease';
            flash.style.opacity = '0';
            setTimeout(() => flash.remove(), 500);
        }, 5000);
    }

    scheduleBracketDraw();
    setTimeout(scheduleBracketDraw, 150);
    setTimeout(scheduleBracketDraw, 400);
    globalThis.addEventListener('resize', scheduleBracketDraw);

    if (globalThis.ResizeObserver) {
        const observer = new ResizeObserver(() => scheduleBracketDraw());
        const wrapperNodes = document.querySelectorAll('.bracket-wrapper');
        for (const wrapper of wrapperNodes) {
            observer.observe(wrapper);
        }
    }
});

// Player profile quick view
function viewPlayerProfile(playerId) {
    globalThis.location.href = `/player/${playerId}`;
}
