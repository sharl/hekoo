## hekoo

'hekoo' is a coined term inspired by the 'heko-obi'

proof of concept with Muse inspired by the amazing invention by @KrisGfx
https://github.com/krisgfx9/Wireless-Device-Battery-Monitor

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
