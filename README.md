## hekoo

'hekoo' is a coined term inspired by the 'heko-obi'

proof of concept with Muse inspired by the amazing invention by @KrisGfx
https://github.com/krisgfx9/Wireless-Device-Battery-Monitor

## Run

```powershell
git clone https://github.com/sharl/hekoo.git
cd hekoo
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
python hekoo.py
```

### Re-run

```powershell
cd hekoo
.\.venv\Scripts\activate
python hekoo.py
```

## Usage

```bash
$ jo text="hello, hekoo" | curl --json @- 192.168.0.1:19876/segments/hekoo
{"ok": true, "name": "hekoo"}
```

![hekoo image](Assets/image.png)

```bash
$ jo segment=$(jo image="$(base64 -w0 emoji_u2603.png)" text=$(jo -a "\-10度" 2cm 1cm/h) ) | curl -s --json @- 192.168.0.1:19876/segments/weather
{"ok": true, "name": "weather"}
```

![hekoo weather](Assets/weather.png)

### SEE ALSO

Examples use [jo](https://github.com/jpmens/jo) to build JSON from shell arguments.  
Plain JSON works too: curl --json '{"text": "hello, hekoo"}' ...

https://github.com/jpmens/jo
