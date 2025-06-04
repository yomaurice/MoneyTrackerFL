// initDb.js
const pool = require('./db');

async function init() {
  try {
    await pool.query(`
      CREATE TABLE IF NOT EXISTS categories (
        id SERIAL PRIMARY KEY,
        name VARCHAR(100) NOT NULL,
        type VARCHAR(10) NOT NULL CHECK (type IN ('income', 'expense'))
      );
    `);

    await pool.query(`
      CREATE TABLE IF NOT EXISTS transactions (
        id SERIAL PRIMARY KEY,
        type VARCHAR(10) NOT NULL CHECK (type IN ('income', 'expense')),
        category VARCHAR(100) NOT NULL,
        amount NUMERIC(10, 2) NOT NULL,
        description TEXT,
        date DATE NOT NULL
      );
    `);

    console.log('Tables created successfully.');
    process.exit();
  } catch (err) {
    console.error('Error initializing DB:', err);
    process.exit(1);
  }
}

init();
