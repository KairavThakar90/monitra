import base64

with open('scratch/official_logo_transparent.png', 'rb') as f:
    b64 = base64.b64encode(f.read()).decode('utf-8')
data_uri = f'data:image/png;base64,{b64}'

html_path = r'C:\Users\Admin\.gemini\antigravity\brain\292fdc6f-99bd-40ae-863d-4a2bca445dba\popup_preview.html'
with open(html_path, 'r', encoding='utf-8') as f:
    content = f.read()

import re
old_section = re.search(r'<!-- Monitra Logo & Brand -->.*?</div>\s*</div>', content, re.DOTALL)
if old_section:
    new_brand = f'''<!-- Official Monitra Logo -->
      <div class="flex items-center justify-center mb-4">
        <img src="{data_uri}" alt="Monitra Logo" class="h-12 w-auto object-contain" />
      </div>'''
    content = content[:old_section.start()] + new_brand + content[old_section.end():]
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(content)
    print('Successfully updated popup_preview.html with official base64 logo')
else:
    print('Logo section not found')

