export default async function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

  if (req.method === 'OPTIONS') {
    return res.status(200).end();
  }

  if (req.method === 'POST') {
    const { user_id, date, pair_number, is_skipped } = req.body || {};
    return res.status(200).json({
      status: 'ok',
      user_id: user_id || null,
      date: date || '',
      pair_number: pair_number || null,
      is_skipped: !!is_skipped
    });
  }

  return res.status(200).json({ status: 'ok' });
}
