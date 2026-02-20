# Jobs CLI Documentation

A simple, clean command-line interface for managing the Django-MOJO Jobs System.

## Philosophy: KISS (Keep It Simple Stupid)

The jobs CLI was designed with extreme simplicity in mind. Instead of complex configuration files, command-line options, and nested subcommands, we provide:

- **Settings-driven configuration**: All configuration comes from Django settings
- **Auto-generated PID files**: No need to manage PID file paths
- **Intuitive commands**: Simple, memorable command patterns
- **Default quiet mode**: Only shows output when needed (use `-v` for verbose)
- **Smart duplicate prevention**: Won't start multiple instances of the same component

## Installation and Setup

The CLI is located at `mojo/apps/jobs/cli.py` and can be executed in several ways:

```bash
# As a Python module (recommended)
python -m mojo.apps.jobs.cli [options] [command]

# Direct execution
python mojo/apps/jobs/cli.py [options] [command]

# With a shebang wrapper script
./bin/jobs.py [options] [command]
```

## Global Options

| Option | Description |
|--------|-------------|
| `-v, --verbose` | Enable verbose output (default is quiet mode) |
| `--validate` | Validate environment and exit |

## Commands Overview

### Global Commands

| Command | Description |
|---------|-------------|
| `status` | Check status of all daemons |
| `stop` | Stop all running daemons |
| `start` | Start both engine and scheduler as daemons |

### Component Commands

| Command | Description |
|---------|-------------|
| `engine start` | Start just the engine as daemon |
| `engine foreground` | Start just the engine in foreground |
| `engine stop` | Stop just the engine |
| `scheduler start` | Start just the scheduler as daemon |
| `scheduler foreground` | Start just the scheduler in foreground |
| `scheduler stop` | Stop just the scheduler |

## Detailed Command Reference

### Status Command

Check what job system components are currently running:

```bash
python -m mojo.apps.jobs.cli status
python -m mojo.apps.jobs.cli -v status  # verbose output
```

**Example output (quiet mode):**
```
✓ Engine running (PID file: /tmp/job-engine-12345.pid)
✓ Scheduler running (PID file: /tmp/job-scheduler-67890.pid)
```

**Example output (verbose mode):**
```
✓ Redis connection successful
✓ Database connection successful
✓ Job models accessible
✓ Environment validation passed
✓ Engine running (PID file: /tmp/job-engine-12345.pid)
✓ Scheduler running (PID file: /tmp/job-scheduler-67890.pid)
```

### Global Start Command

Start both engine and scheduler as background daemons:

```bash
python -m mojo.apps.jobs.cli start
python -m mojo.apps.jobs.cli -v start  # verbose output
```

This is the most common command for production deployment. It:
- Checks if components are already running (won't start duplicates)
- Starts engine daemon with auto-generated PID file
- Starts scheduler daemon with auto-generated PID file
- Uses channels and log files from Django settings

### Global Stop Command

Stop all running job system daemons:

```bash
python -m mojo.apps.jobs.cli stop
python -m mojo.apps.jobs.cli -v stop  # verbose output
```

This command:
- Finds all engine and scheduler PID files in `/tmp/job-*-*.pid`
- Sends graceful shutdown signals to all processes
- Reports success/failure for each component

### Engine Commands

#### Start Engine as Daemon

```bash
python -m mojo.apps.jobs.cli engine start
```

Starts the job engine as a background daemon process. The engine will:
- Process jobs from Redis queues
- Use channels defined in `JOBS_CHANNELS` setting
- Log to file specified in `JOBS_ENGINE_LOGFILE` setting
- Create PID file at `/tmp/job-engine-{runner_id}.pid`

#### Run Engine in Foreground

```bash
python -m mojo.apps.jobs.cli engine foreground
python -m mojo.apps.jobs.cli -v engine foreground  # verbose output
```

Runs the engine in foreground mode for development/debugging. Features:
- Real-time log output to console
- Graceful shutdown on Ctrl+C
- Shows runner ID and channels being processed
- No PID file created (process runs in foreground)

#### Stop Engine

```bash
python -m mojo.apps.jobs.cli engine stop
```

Stops all running engine instances by:
- Finding all engine PID files
- Sending graceful shutdown signals
- Cleaning up PID files

### Scheduler Commands

#### Start Scheduler as Daemon

```bash
python -m mojo.apps.jobs.cli scheduler start
```

Starts the job scheduler as a background daemon. The scheduler:
- Manages scheduled/recurring jobs
- Uses channels defined in `JOBS_CHANNELS` setting
- Logs to file specified in `JOBS_SCHEDULER_LOGFILE` setting
- Creates PID file at `/tmp/job-scheduler-{scheduler_id}.pid`
- **Important**: Only one scheduler should run cluster-wide

#### Run Scheduler in Foreground

```bash
python -m mojo.apps.jobs.cli scheduler foreground
```

Runs scheduler in foreground for development. Shows warnings about:
- Only running one scheduler per cluster
- Leadership lock acquisition attempts
- Real-time scheduling decisions

#### Stop Scheduler

```bash
python -m mojo.apps.jobs.cli scheduler stop
```

Stops all running scheduler instances safely.

## Environment Validation

The CLI automatically validates the environment before executing commands:

```bash
python -m mojo.apps.jobs.cli --validate
```

Checks:
- ✓ Redis connection
- ✓ Database connection  
- ✓ Job models accessibility

If validation fails, the CLI will exit with an error and specific failure reasons.

## Configuration via Django Settings

The CLI uses Django settings instead of command-line arguments:

```python
# settings.py
JOBS_CHANNELS = ['default', 'high_priority']  # Channels to process
JOBS_ENGINE_LOGFILE = '/var/log/jobs-engine.log'  # Engine log file
JOBS_SCHEDULER_LOGFILE = '/var/log/jobs-scheduler.log'  # Scheduler log file
```

## Production Deployment Examples

### Basic Startup (Most Common)

```bash
# Start everything
python -m mojo.apps.jobs.cli start

# Check status
python -m mojo.apps.jobs.cli status
```

### Cron-based Deployment

The CLI is designed to be cron-friendly. This pattern ensures components stay running:

```bash
# /etc/cron.d/jobs
# Start job system every minute (won't create duplicates)
* * * * * user cd /path/to/project && python -m mojo.apps.jobs.cli start >/dev/null 2>&1

# Check status every hour
0 * * * * user cd /path/to/project && python -m mojo.apps.jobs.cli status
```

### Separate Engine and Scheduler

For high-load environments, you might run components on different servers:

```bash
# Server 1: Run only engine
python -m mojo.apps.jobs.cli engine start

# Server 2: Run only scheduler (remember: only one scheduler cluster-wide!)
python -m mojo.apps.jobs.cli scheduler start
```

### Development/Debugging

```bash
# Run engine in foreground to see real-time logs
python -m mojo.apps.jobs.cli -v engine foreground

# In another terminal, run scheduler in foreground
python -m mojo.apps.jobs.cli -v scheduler foreground
```

## Process Management

### PID Files

- Auto-generated at `/tmp/job-engine-{runner_id}.pid` and `/tmp/job-scheduler-{scheduler_id}.pid`
- Include unique IDs to prevent conflicts
- Automatically cleaned up on graceful shutdown

### Signal Handling

Both engine and scheduler support graceful shutdown via:
- `SIGTERM` - Graceful shutdown (recommended)
- `SIGINT` - Interrupt (Ctrl+C)

### Duplicate Prevention

The CLI includes smart duplicate prevention:
- `is_engine_running()` - Checks for any running engine
- `is_scheduler_running()` - Checks for any running scheduler
- Start commands will skip if already running

## Troubleshooting

### Common Issues

**"No module named django"**
```bash
# Make sure you're in the Django project directory
cd /path/to/your/django/project
export DJANGO_SETTINGS_MODULE=your_project.settings
python -m mojo.apps.jobs.cli status
```

**"Redis connection failed"**
```bash
# Validate environment first
python -m mojo.apps.jobs.cli --validate

# Check Redis is running
redis-cli ping
```

**"Database connection failed"**
```bash
# Run Django database checks
python manage.py check --database

# Validate environment
python -m mojo.apps.jobs.cli --validate
```

### Log Files

Check log files for detailed error information:
- Engine logs: Location specified in `JOBS_ENGINE_LOGFILE` setting
- Scheduler logs: Location specified in `JOBS_SCHEDULER_LOGFILE` setting
- Console output: Use `-v` flag for verbose output

### Process Status

If you see stale PID files:
```bash
# This will show stale PID files
python -m mojo.apps.jobs.cli -v status

# Clean them up
python -m mojo.apps.jobs.cli stop
```

## Integration with Management Commands

The CLI can be used as a standalone script or integrated with Django management commands. The underlying functions are also available for programmatic use:

```python
from mojo.apps.jobs.cli import is_engine_running, is_scheduler_running

if not is_engine_running():
    print("Engine needs to be started")
```

## Design Principles

1. **KISS (Keep It Simple Stupid)**: Minimal complexity, maximum clarity
2. **Settings-driven**: Configuration comes from Django settings, not CLI args
3. **Cron-friendly**: Safe to run repeatedly, won't create duplicates
4. **Production-ready**: Proper daemon support, PID files, signal handling
5. **Development-friendly**: Foreground modes for real-time debugging
6. **Self-documenting**: Clear command names, helpful error messages

This CLI replaces complex management commands with a single, intuitive interface that follows Unix philosophy: do one thing and do it well.