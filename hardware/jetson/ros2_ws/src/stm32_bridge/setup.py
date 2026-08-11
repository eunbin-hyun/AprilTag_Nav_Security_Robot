from setuptools import find_packages, setup

package_name = 'stm32_bridge'

setup(
    name=package_name,
    version='0.1.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='eunbin-hyun',
    maintainer_email='eunbin-hyun@users.noreply.github.com',
    description='Jetson-STM32 UART V2 브리지 (Ackermann 변환, 링크 상태 머신, fake_stm32)',
    license='Apache-2.0',
    extras_require={
        'test': [
            'pytest',
        ],
    },
    entry_points={
        'console_scripts': [
            'bridge_node = stm32_bridge.bridge_node:main',
            'fake_stm32 = stm32_bridge.fake_stm32:main',
        ],
    },
)
