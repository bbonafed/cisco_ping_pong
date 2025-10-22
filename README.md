# Cisco Ping Pong League

A Flask-based web application for managing an 8-week round-robin ping pong league with playoff brackets, best-of-three scoring, and comprehensive admin controls.

## Features

- **Player Registration** - Self-service signup with CEC ID tracking
- **Automated Scheduling** - Round-robin schedule generation for 8 weeks
- **Live Standings** - Real-time rankings based on wins, point differential, and total points
- **Match Reporting** - Best-of-three scoring system with verification
- **Playoff System** - Automatic bracket generation with bye handling and winner advancement
- **Admin Dashboard** - League management, week control, and data reset capabilities
- **Modern UI** - Cisco-branded responsive interface with dark theme

## Tech Stack

- **Backend**: Flask (Python)
- **Database**: SQLite
- **Frontend**: Jinja2 templates, modern CSS with CSS Grid/Flexbox
- **Deployment**: Render.com

## Local Development

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Run the application:
   ```bash
   python app.py
   ```

3. Access at `http://localhost:5000`

## Admin Access

Default admin password: `cisco-secure` (configurable via `ADMIN_PASSWORD` environment variable)

## Deployment

Configured for automatic deployment to Render.com via `render.yaml`.

## License

MIT
