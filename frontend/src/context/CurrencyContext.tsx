'use client';

import { createContext, useContext, useState, useEffect } from 'react';

const defaultCurrency = 'ILS';

const CurrencyContext = createContext({
  currency: defaultCurrency,
  setCurrency: (value: string) => {},
});

export const CurrencyProvider = ({ children }: { children: React.ReactNode }) => {
  const [currency, setCurrency] = useState(defaultCurrency);

  useEffect(() => {
    const stored = localStorage.getItem('selectedCurrency');
    if (stored) setCurrency(stored);
  }, []);

  useEffect(() => {
    localStorage.setItem('selectedCurrency', currency);
  }, [currency]);

  return (
    <CurrencyContext.Provider value={{ currency, setCurrency }}>
      {children}
    </CurrencyContext.Provider>
  );
};

export const useCurrency = () => useContext(CurrencyContext);
