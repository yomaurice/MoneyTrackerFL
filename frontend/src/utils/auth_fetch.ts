export const authFetch = (url: string, options: RequestInit = {}) => {
  const token = localStorage.getItem('token');

  const defaultHeaders: HeadersInit = {
    'Authorization': `Bearer ${token}`,
    'Content-Type': 'application/json',
  };

  return fetch(url, {
    ...options,
    headers: {
      ...defaultHeaders,
      ...(options.headers || {}),
    },
    credentials: 'include', // Important if backend uses cookies/sessions
  });
};

export const logout = () => {
  localStorage.removeItem('token');
  window.location.href = '/'; // or wherever your login/home page is
};