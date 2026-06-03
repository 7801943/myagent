/**
 * auth.js — 用户认证模块
 * 职责：登录表单、Token 管理、登录状态检查、登出
 */
import { state, emit } from './state.js';

const TOKEN_KEY = 'myagent_token';
const USERNAME_KEY = 'myagent_username';

// ── Token 管理 ──

export function getToken() {
    return localStorage.getItem(TOKEN_KEY) || '';
}

export function setToken(token, username) {
    localStorage.setItem(TOKEN_KEY, token);
    localStorage.setItem(USERNAME_KEY, username);
    state.authToken = token;
    state.authUsername = username;
    state.isAuthenticated = true;
}

export function clearAuth() {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USERNAME_KEY);
    state.authToken = '';
    state.authUsername = '';
    state.isAuthenticated = false;
}

export function isLoggedIn() {
    return !!getToken();
}

export function getUsername() {
    return localStorage.getItem(USERNAME_KEY) || '';
}

// ── 初始化 ──

export function initAuth() {
    // 恢复登录状态
    const token = getToken();
    const username = getUsername();
    if (token) {
        state.authToken = token;
        state.authUsername = username;
        state.isAuthenticated = true;
    }

    // 绑定登录表单事件
    const loginForm = document.getElementById('loginForm');
    if (loginForm) {
        loginForm.addEventListener('submit', handleLogin);
    }

    // 绑定登出按钮
    const logoutBtn = document.getElementById('logoutBtn');
    if (logoutBtn) {
        logoutBtn.addEventListener('click', handleLogout);
    }

    // 检查登录状态
    if (state.isAuthenticated) {
        // 验证 token 是否有效
        validateToken().then(function (valid) {
            if (valid) {
                showMainApp();
                emit('auth:ready');
            } else {
                showLoginPage();
            }
        });
    } else {
        showLoginPage();
    }
}

// ── 登录处理 ──

async function handleLogin(e) {
    e.preventDefault();

    const usernameInput = document.getElementById('loginUsername');
    const passwordInput = document.getElementById('loginPassword');
    const errorDiv = document.getElementById('loginError');
    const submitBtn = document.getElementById('loginSubmitBtn');

    const username = usernameInput.value.trim();
    const password = passwordInput.value;

    if (!username || !password) {
        showError(errorDiv, '请输入用户名和密码');
        return;
    }

    // 禁用按钮
    submitBtn.disabled = true;
    submitBtn.textContent = '登录中...';
    hideError(errorDiv);

    try {
        const response = await fetch('/api/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username: username, password: password }),
        });

        const data = await response.json();

        if (data.ok) {
            setToken(data.token, data.username);
            showMainApp();
            emit('auth:ready');
        } else {
            showError(errorDiv, data.error || '登录失败');
        }
    } catch (err) {
        showError(errorDiv, '网络错误，请检查连接');
    } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = '登录';
    }
}

// ── 登出处理 ──

async function handleLogout() {
    const token = getToken();
    if (token) {
        try {
            await fetch('/api/auth/logout', {
                method: 'POST',
                headers: { 'Authorization': 'Bearer ' + token },
            });
        } catch (e) {
            // 忽略错误，前端仍需清理
        }
    }
    clearAuth();
    showLoginPage();
    emit('auth:logout');
}

// ── Token 验证 ──

async function validateToken() {
    const token = getToken();
    if (!token) return false;

    try {
        const response = await fetch('/api/auth/me', {
            headers: { 'Authorization': 'Bearer ' + token },
        });
        if (response.ok) {
            return true;
        } else {
            clearAuth();
            return false;
        }
    } catch (e) {
        // 网络错误时保留 token，等连接恢复后重试
        return true;
    }
}

// ── UI 控制 ──

function showLoginPage() {
    const overlay = document.getElementById('loginOverlay');
    if (overlay) {
        overlay.classList.remove('hidden');
    }
    // 清空密码输入
    const passwordInput = document.getElementById('loginPassword');
    if (passwordInput) {
        passwordInput.value = '';
    }
}

function showMainApp() {
    const overlay = document.getElementById('loginOverlay');
    if (overlay) {
        overlay.classList.add('hidden');
    }
    // 更新用户信息显示
    updateUserInfo();
}

function updateUserInfo() {
    const badge = document.getElementById('userInfoBadge');
    const nameSpan = document.getElementById('userInfoName');
    if (badge && nameSpan) {
        const username = getUsername();
        nameSpan.textContent = username;
        badge.style.display = 'flex';
    }
}

function showError(errorDiv, message) {
    if (errorDiv) {
        errorDiv.textContent = message;
        errorDiv.classList.add('visible');
    }
}

function hideError(errorDiv) {
    if (errorDiv) {
        errorDiv.classList.remove('visible');
    }
}