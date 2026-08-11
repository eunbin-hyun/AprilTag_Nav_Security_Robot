from setuptools import find_packages, setup

package_name = 'web_bridge'

setup(
    name=package_name,
    version='0.0.0',
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
    description='ROS 2 상태를 WebSocket으로 중계하는 웹 연동 브리지',
    license='Apache-2.0',
    extras_require={
        'test': [
            'pytest',
        ],
    },
    entry_points={
        'console_scripts': [
            'web_bridge_node = web_bridge.web_bridge_node:main',
            'fake_web_topics = web_bridge.fake_web_topics:main',
        ],
    },
)
