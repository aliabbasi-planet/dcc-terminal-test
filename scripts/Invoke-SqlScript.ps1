$envPath = Join-Path $PSScriptRoot ".env"

if (-not (Test-Path $envPath)) {
    Write-Host "ERROR: .env file not found at $envPath" -ForegroundColor Red
    exit 1
}

$envVars = @{}
Get-Content $envPath | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
        $parts = $line.Split("=", 2)
        $key   = $parts[0].Trim()
        $value = $parts[1].Trim()
        $envVars[$key] = $value
    }
}

$authMode = $envVars["AUTH_MODE"]
$server   = $envVars["DB_SERVER"]
$database = $envVars["DB_NAME"]

if (-not $server -or -not $database) {
    Write-Host "ERROR: DB_SERVER or DB_NAME missing in .env" -ForegroundColor Red
    exit 1
}

if ($authMode -eq "Windows") {
    $connStr = "Server=$server;Database=$database;Integrated Security=True;TrustServerCertificate=True;"
    $authDescription = "Windows Authentication (your current identity)"
}
elseif ($authMode -eq "Sql") {
    $user     = $envVars["DB_USER"]
    $password = $envVars["DB_PASSWORD"]

    if (-not $user -or -not $password) {
        Write-Host "ERROR: AUTH_MODE=Sql but DB_USER or DB_PASSWORD missing in .env" -ForegroundColor Red
        exit 1
    }

    $connStr = "Server=$server;Database=$database;User ID=$user;Password=$password;TrustServerCertificate=True;"
    $authDescription = "SQL Authentication (user: $user)"
}
else {
    Write-Host "ERROR: AUTH_MODE must be 'Windows' or 'Sql' (found: '$authMode')" -ForegroundColor Red
    exit 1
}

if ($args.Count -gt 0) {
    $sqlFile = $args[0]
    if (-not (Test-Path $sqlFile)) {
        Write-Host "ERROR: SQL file not found: $sqlFile" -ForegroundColor Red
        exit 1
    }
    $query = Get-Content $sqlFile -Raw
    $sourceDescription = $sqlFile
}
else {
    $query = @"
SELECT
    @@SERVERNAME AS server_name,
    CONNECTIONPROPERTY('local_net_address')  AS server_ip,
    CONNECTIONPROPERTY('client_net_address') AS my_client_ip,
    DB_NAME() AS database_name,
    SUSER_SNAME() AS login_name,
    GETDATE() AS server_time;
"@
    $sourceDescription = "default connectivity check"
}

Write-Host "Connecting to $server / $database using $authDescription ..." -ForegroundColor Cyan
Write-Host "Running: $sourceDescription" -ForegroundColor Cyan

try {
    $conn = New-Object System.Data.SqlClient.SqlConnection $connStr
    $conn.Open()

    $cmd = $conn.CreateCommand()
    $cmd.CommandText = $query
    $reader = $cmd.ExecuteReader()

    $table = New-Object System.Data.DataTable
    $table.Load($reader)

    if ($table.Rows.Count -gt 0) {
        $table | Format-Table -AutoSize
    }
    else {
        Write-Host "(query ran, no rows returned)" -ForegroundColor Yellow
    }

    $conn.Close()
    Write-Host "`n SUCCESS - connected and executed against $server / $database" -ForegroundColor Green
}
catch {
    Write-Host "`n FAILED: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}