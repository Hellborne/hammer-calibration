# Калибровка Hammer · Hammer Calibration

### ⬇️ [Скачать для Windows · Download for Windows](https://github.com/Hellborne/hammer-calibration/releases/latest/download/HammerCalibration.exe)

[Русский](#русский) · [English](#english)

## Русский

Программа для калибровки (spindown) умного станка Saris / CycleOps Hammer с компьютера на Windows, по ANT+ донглу или по Bluetooth. На экране крупно видна скорость и что делать: разгоняйся, держи, перестань крутить. Перед калибровкой 10 минут прогрева: холодный станок тормозит сильнее, и время выбега выходит другим.

<img src="docs/coast-ru.png" width="49%" alt="Красный экран «ПЕРЕСТАНЬ КРУТИТЬ» с текущей скоростью"> <img src="docs/done-ru.png" width="49%" alt="Зелёный экран «ГОТОВО» с новым временем выбега">

### Скачать и запустить

1. Нажми [Скачать HammerCalibration.exe](https://github.com/Hellborne/hammer-calibration/releases/latest/download/HammerCalibration.exe), файл скачается сразу. Он же лежит на странице [Releases](../../releases/latest) в разделе Assets. Зелёная кнопка «Code → Download ZIP» и файлы «Source code» — это исходники для программистов, для запуска они не нужны.
2. Если браузер пишет, что такой файл обычно не скачивают или он может быть опасен, всё равно сохрани его. В Edge открой загрузки, нажми «…» рядом с файлом, потом «Сохранить», а если спросит ещё раз, «Показать больше» и «Всё равно сохранить». В Chrome нажми «Сохранить».
3. Положи файл в любую папку, например на рабочий стол, и запусти двойным щелчком. Ставить Python и что-то ещё не нужно.
4. Если Windows покажет «Система Windows защитила ваш компьютер», нажми «Подробнее», потом «Выполнить в любом случае».

Браузер и Windows так встречают любую новую программу без платной цифровой подписи. Если ругается антивирус, это ложное срабатывание: так бывает с программами, собранными из Python. Весь исходный код открыт в этом репозитории, а exe из него собирает `build.bat`.

### Что нужно

- Windows 10 или 11, 64-бит.
- Станок включён в розетку.
- ANT+ USB-донгл (такой же, как для Zwift) **или** Bluetooth в компьютере.
- Драйвер донгла ставится вместе с Zwift: если Zwift на этом компьютере есть, всё уже готово.
- Перед запуском закрой Zwift и приложения на телефоне: донгл и станок работают только с одной программой сразу.
- Около 12 минут: 10 минут прогрева и пара минут на саму калибровку.

### Как откалибровать

1. Выбери связь, ANT+ донгл или Bluetooth, и нажми **Калибровать**.
2. Крути спокойно 10 минут прогрева. Если станок уже тёплый, нажми **Калибровать сейчас** или пробел.
3. Делай, что написано на экране: разгоняйся до целевой скорости (обычно 32–34 км/ч, программа покажет точную) и держи её, а на красном экране **ПЕРЕСТАНЬ КРУТИТЬ** отпусти педали и жди.
4. На зелёном экране **ГОТОВО** будет новое время выбега. Можно запускать Zwift.

Кнопка **Проверить связь** только показывает данные станка и ничего в нём не меняет.

### Частые вопросы

**Какое время выбега правильное?** Число у каждого станка своё, сравнивать его с чужим нет смысла. Важно, чтобы у твоего станка оно не скакало от раза к разу. Поэтому калибруй прогретый станок и всегда одним способом, лучше по ANT+: так станок сам проверяет, что прогрелся.

**Как часто калибровать?** После перевозки станка, когда в комнате заметно поменялась температура, или если мощность кажется странной. Перед каждой тренировкой не нужно.

### Если не работает

- **Донгл не найден.** Закрой Zwift и вставь донгл в другой USB-порт. Если Zwift на компьютере нет, поставь его: вместе с ним ставится драйвер донгла.
- **Станок не найден.** Покрути педали, чтобы он проснулся, выключи Bluetooth на телефоне, поднеси донгл ближе к станку (поможет USB-удлинитель).
- **Калибровка не удалась.** Держи скорость ровно, а после «ПЕРЕСТАНЬ КРУТИТЬ» не трогай педали до конца выбега.
- Рядом с программой появляется файл **hammer log.txt** с подробностями. Его можно приложить к вопросу в [Issues](../../issues). В логе есть серийный номер станка и его Bluetooth-адрес: если не хочешь их показывать, сотри эти строки.

### Какие станки

Проверено на Saris Hammer (модель 320, прошивка 31.065). H2 и H3, скорее всего, тоже подойдут: по ANT+ калибровка стандартная, а по Bluetooth приложение Saris калибрует все свои станки одинаково. Но на них программа ещё не проверялась. Если попробуешь, напиши в [Issues](../../issues), получилось или нет, и приложи лог.

### Язык

Русский или английский программа выбирает сама по языку Windows. Переключить можно внизу окна, выбор запоминается.

### Запуск из исходников

Нужен Python 3.12:

```
pip install -r requirements.txt
python "hammer spindown.py"
```

Или запусти `запуск калибровки.bat`: он сам поставит библиотеки. Ключи командной строки: `--info` проверить связь в консоли, `--console` калибровать в консоли, `--ant` / `--ble` сразу выбрать связь, `--no-warmup` без прогрева, `--en` / `--ru` язык.

Собрать exe: `build.bat`. Нужен Python 3.12 с python.org (с Anaconda exe не запустится на других компьютерах), готовый файл появится в папке `dist`.

### Важно

Программа сделана независимо и никак не связана с Saris. Она запускает ту же калибровку, что официальное приложение Saris и Zwift, но пользуешься ей на свой риск. Лицензия MIT.

## English

A Windows program that runs the spindown calibration of a Saris / CycleOps Hammer smart trainer over an ANT+ dongle or Bluetooth. The screen shows your speed in large digits and what to do: speed up, hold, stop pedaling. Before the calibration there is a 10-minute warm-up: a cold trainer has more drag and gives a different spindown time.

<img src="docs/coast-en.png" width="49%" alt="Red STOP PEDALING screen with the current speed"> <img src="docs/done-en.png" width="49%" alt="Green DONE screen with the new spindown time">

### Download and run

1. Click [Download HammerCalibration.exe](https://github.com/Hellborne/hammer-calibration/releases/latest/download/HammerCalibration.exe) and the file downloads right away. It is also on the [Releases](../../releases/latest) page under Assets. The green "Code → Download ZIP" button and the "Source code" files are the source for programmers: you don't need them to run the program.
2. If the browser says the file is not commonly downloaded or may be dangerous, keep it anyway. In Edge, open the downloads, click "…" next to the file, then "Keep", and if it asks again, "Show more" and "Keep anyway". In Chrome, click "Keep".
3. Put the file in any folder, for example on the desktop, and double-click it. No need to install Python or anything else.
4. If Windows says "Windows protected your PC", click "More info", then "Run anyway".

Browsers and Windows treat any new program without a paid code-signing certificate this way. If your antivirus complains, it is a false positive, which sometimes happens with programs built from Python. All the source code is open in this repository, and `build.bat` builds the exe from it.

### What you need

- Windows 10 or 11, 64-bit.
- The trainer plugged into power.
- An ANT+ USB dongle (the same kind as for Zwift) **or** Bluetooth on the computer.
- The dongle driver comes with Zwift: if Zwift is installed on this computer, you are all set.
- Close Zwift and the apps on your phone first: the dongle and the trainer work with one program at a time.
- About 12 minutes: a 10-minute warm-up and a couple of minutes for the calibration itself.

### How to calibrate

1. Choose the connection, ANT+ dongle or Bluetooth, and click **Calibrate**.
2. Pedal easily through the 10-minute warm-up. If the trainer is already warm, click **Calibrate now** or press Space.
3. Follow the screen: speed up to the target speed and hold it, and on the red **STOP PEDALING** screen let go of the pedals and wait. The program shows speed in km/h: the target is usually 32–34 km/h, about 20–21 mph, and the screen shows the exact value.
4. The green **DONE** screen shows the new spindown time. You can start Zwift.

**Check connection** only shows the trainer's data and changes nothing in it.

### FAQ

**What spindown time is right?** Every trainer has its own number, so comparing yours with someone else's makes no sense. What matters is that your trainer's number stays steady from one calibration to the next. So calibrate a warm trainer and always the same way, preferably over ANT+: then the trainer checks for itself that it has warmed up.

**How often should I calibrate?** After moving the trainer, when the room temperature has changed noticeably, or when the power seems off. Not before every ride.

### If something goes wrong

- **No dongle found.** Close Zwift and plug the dongle into another USB port. If Zwift is not installed, install it: the dongle driver comes with it.
- **No trainer found.** Turn the pedals to wake it up, turn off Bluetooth on your phone, move the dongle closer to the trainer (a USB extension cable helps).
- **Calibration failed.** Hold the speed steady, and after "STOP PEDALING" don't touch the pedals until the spindown ends.
- The program writes **hammer log.txt** next to itself with the details. You can attach it to a question in [Issues](../../issues). The log contains the trainer's serial number and Bluetooth address: delete those lines if you don't want to share them.

### Which trainers

Tested on a Saris Hammer (model 320, firmware 31.065). The H2 and H3 will most likely work too: over ANT+ the calibration is standard, and over Bluetooth the Saris app calibrates all its trainers the same way. But the program has not been tried on them yet. If you try it, please report in [Issues](../../issues) whether it worked, with the log.

### Language

The program picks Russian or English from the Windows language. You can switch it at the bottom of the window, and the choice is remembered.

### Running from source

You need Python 3.12:

```
pip install -r requirements.txt
python "hammer spindown.py"
```

Command line options: `--info` check the connection in the console, `--console` calibrate in the console, `--ant` / `--ble` choose the connection, `--no-warmup` skip the warm-up, `--en` / `--ru` language.

To build the exe, run `build.bat`. It needs Python 3.12 from python.org (an exe built with Anaconda won't start on other computers), and the result goes to the `dist` folder.

### Disclaimer

This is an independent project, not affiliated with Saris. It runs the same calibration as the official Saris app and Zwift, but you use it at your own risk. MIT license.
